# frozen_string_literal: true

require 'pathname'
require_relative 'verification'

module DccMcp
  module SketchupAdapter
    class Commands
      MAX_LIST_ITEMS = 500
      MAX_OPTION_KEYS = 64
      MAX_PROBE_SYMBOLS = 128
      ADAPTER_VERSION = '0.2.0' # x-release-please-version
      OPTION_KEY_PATTERN = /\A[a-z][a-z0-9_]{0,63}\z/.freeze
      PROBE_ENTRY_PATTERN = /\A[A-Z][A-Za-z0-9]*(?:::[A-Z][A-Za-z0-9]*)*[#.][A-Za-z_][A-Za-z0-9_]*[?!=]?\z/.freeze
      IMPORT_EXTENSIONS = %w[.3ds .dae .dwg .dxf .ifc .kmz .obj .skp .stl].freeze
      EXPORT_EXTENSIONS = %w[.3ds .dae .dwg .dxf .fbx .glb .ifc .kmz .obj .pdf .usdz .wrl .xsi].freeze
      UNIT_FACTORS = {
        'inches' => 1.0,
        'feet' => 12.0,
        'millimeters' => 1.0 / 25.4,
        'centimeters' => 1.0 / 2.54,
        'meters' => 1.0 / 0.0254
      }.freeze

      def initialize
        @commands = {
          'diagnostics.ping' => method(:ping),
          'diagnostics.api_probe' => method(:api_probe),
          'model.inspect' => method(:inspect_model),
          'model.list_entities' => method(:list_entities),
          'model.save' => method(:save_model),
          'model.save_copy' => method(:save_copy),
          'model.validate' => method(:validate_model),
          'model.import' => method(:import_model),
          'model.export' => method(:export_model),
          'geometry.add_box' => method(:add_box),
          'geometry.add_cylinder' => method(:add_cylinder),
          'geometry.group' => method(:group_entities),
          'entity.transform' => method(:transform_entity),
          'entity.rename' => method(:rename_entity),
          'entity.erase' => method(:erase_entities),
          'entity.select' => method(:select_entities),
          'materials.list' => method(:list_materials),
          'materials.create' => method(:create_material),
          'materials.update' => method(:update_material),
          'materials.assign' => method(:assign_material),
          'materials.remove' => method(:remove_material),
          'scenes.list' => method(:list_scenes),
          'scenes.create' => method(:create_scene),
          'scenes.update' => method(:update_scene),
          'scenes.remove' => method(:remove_scene),
          'tags.list' => method(:list_tags),
          'tags.create' => method(:create_tag),
          'tags.assign' => method(:assign_tag),
          'tags.remove' => method(:remove_tag)
        }.freeze
      end

      def execute(command_name, params)
        command = @commands[command_name]
        raise ArgumentError, "Unsupported SketchUp command: #{command_name}" unless command

        values = params.dup
        deadline = values.delete('_dcc_mcp_deadline_unix_ms')
        if deadline && integer(deadline, 'deadline') < (Time.now.to_f * 1000).to_i
          raise 'SketchUp request expired before main-thread execution'
        end
        command.call(values)
      end

      private

      def ping(params)
        require_keys(params, [])
        {
          'status' => 'ok',
          'sketchup_version' => Sketchup.version.to_s,
          'ruby_version' => RUBY_VERSION.to_s,
          'host_pid' => Process.pid,
          'adapter_version' => ADAPTER_VERSION,
          'plugin_path' => File.expand_path(__dir__),
          'host_thread_id' => Thread.current.object_id,
          'command_count' => @commands.length,
          'model_valid' => model.valid?
        }
      end

      # Answer "does this host expose the API the adapter depends on?" without
      # the Ruby side owning a second copy of the list. The caller (the Python
      # doctor) reads the symbols out of compat_matrix.json and sends them, so
      # the matrix stays the single source of truth across both runtimes.
      def api_probe(params)
        require_keys(params, %w[symbols], %w[symbols])
        symbols = params['symbols']
        raise ArgumentError, 'symbols must be an array' unless symbols.is_a?(Array)
        raise ArgumentError, "symbols must contain at most #{MAX_PROBE_SYMBOLS} entries" if symbols.length > MAX_PROBE_SYMBOLS

        {
          'status' => 'ok',
          'sketchup_version' => Sketchup.version.to_s,
          'ruby_version' => RUBY_VERSION.to_s,
          'probe' => symbols.map { |entry| probe_entry(entry) }
        }
      end

      def inspect_model(params)
        require_keys(params, [])
        entities = model.entities.to_a
        type_counts = entities.each_with_object(Hash.new(0)) do |entity, counts|
          counts[entity.typename.to_s] += 1
        end
        {
          'title' => model.title.to_s,
          'name' => model.name.to_s,
          'path' => model.path.to_s,
          'guid' => model.guid.to_s,
          'modified' => model.modified?,
          'valid' => model.valid?,
          'root_entity_count' => entities.length,
          'root_entity_types' => type_counts.sort.to_h,
          'material_count' => model.materials.length,
          'scene_count' => model.pages.length,
          'tag_count' => model.layers.length,
          'selection_count' => model.selection.length,
          'active_edit_depth' => Array(model.active_path).length,
          'bounds' => bounds_summary(model.bounds)
        }
      end

      def list_entities(params)
        require_keys(params, %w[kind limit])
        kind = optional_string(params['kind'])
        limit = params.key?('limit') ? integer(params['limit'], 'limit', 1, MAX_LIST_ITEMS) : 200
        values = model.entities.to_a
        values.select! { |entity| entity.typename.to_s.casecmp?(kind) } if kind
        {
          'entities' => values.first(limit).map { |entity| entity_summary(entity) },
          'total' => values.length,
          'truncated' => values.length > limit,
          'scope' => 'root'
        }
      end

      def save_model(params)
        require_keys(params, %w[path overwrite])
        overwrite = params.key?('overwrite') ? boolean(params['overwrite'], 'overwrite') : false
        path = params.key?('path') ? output_file(params['path'], 'path', ['.skp']) : nil
        reject_existing(path, overwrite) if path && !same_path?(path, current_model_path)
        result = path ? model.save(path) : model.save
        raise 'SketchUp did not save the model' unless result

        saved_path = model.path.to_s.empty? ? path.to_s : model.path.to_s
        checks = []
        Verification.check_file_written!('model.save', checks, saved_path)
        Verification.check!('model.save', checks, 'model_not_modified', false, model.modified?)
        { 'path' => model.path.to_s, 'saved' => true, 'modified' => model.modified? }
          .merge(Verification.verified('model.save', checks))
      end

      def save_copy(params)
        require_keys(params, %w[path overwrite], %w[path])
        overwrite = params.key?('overwrite') ? boolean(params['overwrite'], 'overwrite') : false
        path = output_file(params['path'], 'path', ['.skp'])
        reject_existing(path, overwrite)
        result = model.save_copy(path)
        raise 'SketchUp did not save the model copy' unless result

        checks = []
        Verification.check_file_written!('model.save_copy', checks, path)
        { 'path' => path, 'saved' => true, 'current_model_path' => model.path.to_s }
          .merge(Verification.verified('model.save_copy', checks))
      end

      def validate_model(params)
        require_keys(params, [])
        invalid = model.entities.to_a.reject(&:valid?)
        {
          'valid' => model.valid? && invalid.empty?,
          'invalid_root_entity_ids' => invalid.map { |entity| persistent_id(entity) },
          'modified' => model.modified?,
          'root_entity_count' => model.entities.length
        }
      end

      def import_model(params)
        require_keys(params, %w[path options], %w[path])
        path = input_file(params['path'], 'path', IMPORT_EXTENSIONS)
        options = symbol_keyed_options(params.fetch('options', {}), 'options')
        before_count = model.entities.length
        result = options.empty? ? model.import(path) : model.import(path, options)
        raise "SketchUp import failed: #{path}" unless result

        after_count = model.entities.length
        checks = []
        # Import must not remove geometry. A count increase is the expected
        # outcome but is not asserted, because a legitimate import can add
        # nothing to the root context (for example a file that imports entirely
        # into a definition). Losing entities, however, is never legitimate.
        Verification.check_count_not_decreased!('model.import', checks, before_count, after_count)
        Verification.check!('model.import', checks, 'source_file_readable', true, File.file?(path))
        {
          'path' => path, 'imported' => true, 'root_entity_count' => after_count
        }.merge(Verification.verified('model.import', checks))
      end

      def export_model(params)
        require_keys(params, %w[path options overwrite], %w[path])
        overwrite = params.key?('overwrite') ? boolean(params['overwrite'], 'overwrite') : false
        path = output_file(params['path'], 'path', EXPORT_EXTENSIONS)
        reject_existing(path, overwrite)
        options = symbol_keyed_options(params.fetch('options', {}), 'options')
        result = options.empty? ? model.export(path) : model.export(path, options)
        raise "SketchUp export failed: #{path}" unless result

        checks = []
        Verification.check_file_written!('model.export', checks, path)
        { 'path' => path, 'exported' => true, 'exists' => File.file?(path) }
          .merge(Verification.verified('model.export', checks))
      end

      def add_box(params)
        require_keys(params, %w[origin width depth height unit name], %w[width depth height])
        unit = unit_name(params.fetch('unit', 'meters'))
        origin = point(params.fetch('origin', [0, 0, 0]), 'origin', unit)
        width = positive_length(params['width'], 'width', unit)
        depth = positive_length(params['depth'], 'depth', unit)
        height = positive_length(params['height'], 'height', unit)
        name = optional_string(params['name']) || 'DCC-MCP Box'
        created = nil
        checks = []
        with_operation('DCC-MCP Add Box') do
          group = model.entities.add_group
          group.name = name
          points = [
            origin,
            offset_point(origin, width, 0, 0),
            offset_point(origin, width, depth, 0),
            offset_point(origin, 0, depth, 0)
          ]
          face = group.entities.add_face(points)
          raise 'SketchUp could not create the box base face' unless face

          face.reverse! if face.normal.z.negative?
          face.pushpull(height)
          created = group
          verify_added_geometry('geometry.add_box', checks, group, name, [width, depth, height])
        end
        { 'entity' => entity_summary(created), 'unit' => unit }
          .merge(Verification.verified('geometry.add_box', checks))
      end

      def add_cylinder(params)
        require_keys(
          params,
          %w[center radius height segments unit name],
          %w[radius height]
        )
        unit = unit_name(params.fetch('unit', 'meters'))
        center = point(params.fetch('center', [0, 0, 0]), 'center', unit)
        radius = positive_length(params['radius'], 'radius', unit)
        height = positive_length(params['height'], 'height', unit)
        segments = integer(params.fetch('segments', 24), 'segments', 3, 128)
        name = optional_string(params['name']) || 'DCC-MCP Cylinder'
        created = nil
        checks = []
        with_operation('DCC-MCP Add Cylinder') do
          group = model.entities.add_group
          group.name = name
          edges = group.entities.add_circle(center, [0, 0, 1], radius, segments)
          face = group.entities.add_face(edges)
          raise 'SketchUp could not create the cylinder base face' unless face

          face.reverse! if face.normal.z.negative?
          face.pushpull(height)
          created = group
          verify_added_geometry(
            'geometry.add_cylinder', checks, group, name,
            [radius * 2, radius * 2, height]
          )
        end
        { 'entity' => entity_summary(created), 'segments' => segments, 'unit' => unit }
          .merge(Verification.verified('geometry.add_cylinder', checks))
      end

      # Shared read-back for the two primitives built by push-pulling a base
      # face. It proves three independent things: the group survives a round
      # trip through the persistent id index, it carries the requested name, and
      # the bounding box has the requested extents -- the last one is what
      # catches a pushpull that silently produced no volume.
      def verify_added_geometry(tool, checks, group, name, expected_extents)
        identifier = persistent_id(group)
        found = Verification.check_entity_present!(tool, checks, model, identifier)
        return if found.nil?

        Verification.check_entity_name!(tool, checks, found, name)
        Verification.check_extents!(tool, checks, found, expected_extents.map(&:to_f))
      end

      def group_entities(params)
        require_keys(params, %w[entity_ids name], %w[entity_ids])
        entities = root_entities(params['entity_ids'])
        name = optional_string(params['name']) || 'DCC-MCP Group'
        created = nil
        checks = []
        with_operation('DCC-MCP Group Entities') do
          created = model.entities.add_group(entities)
          created.name = name
          identifier = persistent_id(created)
          found = Verification.check_entity_present!('geometry.group', checks, model, identifier)
          unless found.nil?
            Verification.check_entity_name!('geometry.group', checks, found, name)
            Verification.check_group_members!('geometry.group', checks, found, entities.length)
          end
        end
        { 'entity' => entity_summary(created), 'grouped_count' => entities.length }
          .merge(Verification.verified('geometry.group', checks))
      end

      def transform_entity(params)
        require_keys(
          params,
          %w[entity_id translation rotation_axis rotation_degrees scale unit],
          %w[entity_id]
        )
        entity = root_entity(params['entity_id'])
        unit = unit_name(params.fetch('unit', 'meters'))
        translation = vector(params.fetch('translation', [0, 0, 0]), 'translation', unit)
        axis = numeric_triplet(params.fetch('rotation_axis', [0, 0, 1]), 'rotation_axis')
        raise ArgumentError, 'rotation_axis must not be zero' if axis.all?(&:zero?)

        degrees = number(params.fetch('rotation_degrees', 0), 'rotation_degrees', -360_000, 360_000)
        scale = scale_triplet(params.fetch('scale', [1, 1, 1]))
        center = entity.respond_to?(:bounds) ? entity.bounds.center : Geom::Point3d.new(0, 0, 0)
        before_center = entity.respond_to?(:bounds) ? Verification.centre(entity.bounds) : nil
        transformation = Geom::Transformation.translation(translation)
        transformation *= Geom::Transformation.rotation(center, axis, degrees.degrees)
        transformation *= Geom::Transformation.scaling(center, *scale)
        checks = []
        with_operation('DCC-MCP Transform Entity') do
          unless model.entities.transform_entities(transformation, [entity])
            raise "SketchUp could not transform entity: #{persistent_id(entity)}"
          end

          # transform_entities returns an entity array on some builds and a
          # truthy count on others, so the return value is not a success
          # signal. The read-back is: rotation and scaling are applied about
          # the entity centre, which leaves the centre invariant, so the centre
          # must have moved by exactly the requested translation.
          if before_center && entity.respond_to?(:bounds)
            Verification.check_center_moved!(
              'entity.transform', checks, entity, before_center, translation
            )
          else
            Verification.check!('entity.transform', checks, 'entity_present', true, entity.valid?)
          end
        end
        { 'entity' => entity_summary(entity), 'unit' => unit }
          .merge(Verification.verified('entity.transform', checks))
      end

      def rename_entity(params)
        require_keys(params, %w[entity_id name], %w[entity_id name])
        entity = entity(params['entity_id'])
        raise ArgumentError, 'entity does not support names' unless entity.respond_to?(:name=)

        name = non_empty_string(params['name'], 'name')
        checks = []
        with_operation('DCC-MCP Rename Entity') do
          entity.name = name
          Verification.check_entity_present!(
            'entity.rename', checks, model, persistent_id(entity)
          )
          Verification.check_entity_name!('entity.rename', checks, entity, name)
        end
        { 'entity' => entity_summary(entity) }
          .merge(Verification.verified('entity.rename', checks))
      end

      def erase_entities(params)
        require_keys(params, %w[entity_ids], %w[entity_ids])
        entities = root_entities(params['entity_ids'])
        ids = entities.map { |item| persistent_id(item) }
        checks = []
        with_operation('DCC-MCP Erase Entities') do
          model.entities.erase_entities(entities)
          Verification.check_absent!('entity.erase', checks, model, ids, 'entity')
        end
        { 'erased_entity_ids' => ids, 'erased_count' => ids.length }
          .merge(Verification.verified('entity.erase', checks))
      end

      def select_entities(params)
        require_keys(params, %w[entity_ids replace], %w[entity_ids])
        entities = entities(params['entity_ids'])
        replace = params.key?('replace') ? boolean(params['replace'], 'replace') : true
        requested = entities.map { |item| persistent_id(item) }
        checks = []
        model.selection.clear if replace
        model.selection.add(entities)
        # Selection is not wrapped in an undo operation: SketchUp does not
        # commit selection changes as a model operation, and aborting one would
        # discard nothing while reporting a rollback that never happened.
        Verification.check_selection!('entity.select', checks, model, requested)
        {
          'selected_entity_ids' => model.selection.map { |item| persistent_id(item) },
          'selection_count' => model.selection.length
        }.merge(Verification.verified('entity.select', checks))
      end

      def list_materials(params)
        require_keys(params, [])
        values = model.materials.map { |material| material_summary(material) }
        { 'materials' => values, 'total' => values.length }
      end

      def create_material(params)
        require_keys(params, %w[name color alpha texture_path], %w[name])
        name = non_empty_string(params['name'], 'name')
        raise ArgumentError, "material already exists: #{name}" if find_material(name, false)

        created = nil
        checks = []
        with_operation('DCC-MCP Create Material') do
          created = model.materials.add(name)
          apply_material_values(created, params)
          Verification.check_material!(
            'materials.create', checks, model, name, material_expectations(params)
          )
          if params.key?('texture_path')
            Verification.check_material_texture!('materials.create', checks, model, name)
          end
        end
        { 'material' => material_summary(created) }
          .merge(Verification.verified('materials.create', checks))
      end

      # The values a material must report back after create/update. Only the
      # fields the caller actually supplied are asserted: a read-back that
      # demanded defaults would fail on any host that fills them differently.
      def material_expectations(params)
        values = {}
        values['color'] = color_values(params['color']) if params.key?('color')
        values['alpha'] = number(params['alpha'], 'alpha', 0, 1).to_f if params.key?('alpha')
        values
      end

      def update_material(params)
        require_keys(
          params,
          %w[name new_name color alpha texture_path clear_texture],
          %w[name]
        )
        material = find_material(params['name'])
        expected_name = params.key?('new_name') ? non_empty_string(params['new_name'], 'new_name') : material.name.to_s
        checks = []
        with_operation('DCC-MCP Update Material') do
          if params.key?('new_name')
            new_name = non_empty_string(params['new_name'], 'new_name')
            existing = find_material(new_name, false)
            raise ArgumentError, "material already exists: #{new_name}" if existing && existing != material

            material.name = new_name
          end
          if params.key?('clear_texture') && boolean(params['clear_texture'], 'clear_texture')
            material.texture = nil
          end
          apply_material_values(material, params)
          Verification.check_material!(
            'materials.update', checks, model, expected_name, material_expectations(params)
          )
          if params.key?('clear_texture') && boolean(params['clear_texture'], 'clear_texture')
            Verification.check_material_texture!(
              'materials.update', checks, model, expected_name, false
            )
          elsif params.key?('texture_path')
            Verification.check_material_texture!('materials.update', checks, model, expected_name)
          end
        end
        { 'material' => material_summary(material) }
          .merge(Verification.verified('materials.update', checks))
      end

      def assign_material(params)
        require_keys(params, %w[entity_id material side], %w[entity_id material])
        item = entity(params['entity_id'])
        material = find_material(params['material'])
        side = params.fetch('side', 'front').to_s
        raise ArgumentError, 'side must be front, back, or both' unless %w[front back both].include?(side)
        if %w[back both].include?(side) && !item.respond_to?(:back_material=)
          raise ArgumentError, 'back material is supported only by face-like entities'
        end

        checks = []
        with_operation('DCC-MCP Assign Material') do
          item.material = material if %w[front both].include?(side)
          item.back_material = material if %w[back both].include?(side)
          Verification.check_assigned_material!('materials.assign', checks, item, material, side)
        end
        { 'entity' => entity_summary(item), 'material' => material_summary(material), 'side' => side }
          .merge(Verification.verified('materials.assign', checks))
      end

      def remove_material(params)
        require_keys(params, %w[name], %w[name])
        material = find_material(params['name'])
        raise ArgumentError, "material is in use: #{material.name}" if material_in_use?(material)

        name = material.name.to_s
        checks = []
        with_operation('DCC-MCP Remove Material') do
          raise "SketchUp could not remove material: #{name}" unless model.materials.remove(material)

          Verification.check_material_absent!('materials.remove', checks, model, name)
        end
        { 'removed_material' => name }
          .merge(Verification.verified('materials.remove', checks))
      end

      def list_scenes(params)
        require_keys(params, [])
        values = model.pages.map { |page| scene_summary(page) }
        { 'scenes' => values, 'total' => values.length }
      end

      def create_scene(params)
        require_keys(params, %w[name description include_in_animation], %w[name])
        name = non_empty_string(params['name'], 'name')
        raise ArgumentError, "scene already exists: #{name}" if find_scene(name, false)

        created = nil
        checks = []
        expected_description = params['description'].to_s if params.key?('description')
        with_operation('DCC-MCP Create Scene') do
          created = model.pages.add(name)
          created.description = params['description'].to_s if params.key?('description')
          if params.key?('include_in_animation') && created.respond_to?(:include_in_animation=)
            created.include_in_animation = boolean(params['include_in_animation'], 'include_in_animation')
          end
          Verification.check_scene!('scenes.create', checks, model, name, expected_description)
        end
        { 'scene' => scene_summary(created) }
          .merge(Verification.verified('scenes.create', checks))
      end

      def update_scene(params)
        require_keys(
          params,
          %w[name new_name description include_in_animation capture_current_view],
          %w[name]
        )
        page = find_scene(params['name'])
        expected_name = params.key?('new_name') ? non_empty_string(params['new_name'], 'new_name') : page.name.to_s
        expected_description = params['description'].to_s if params.key?('description')
        checks = []
        with_operation('DCC-MCP Update Scene') do
          if params.key?('new_name')
            new_name = non_empty_string(params['new_name'], 'new_name')
            existing = find_scene(new_name, false)
            raise ArgumentError, "scene already exists: #{new_name}" if existing && existing != page

            page.name = new_name
          end
          page.description = params['description'].to_s if params.key?('description')
          if params.key?('include_in_animation') && page.respond_to?(:include_in_animation=)
            page.include_in_animation = boolean(params['include_in_animation'], 'include_in_animation')
          end
          if params.key?('capture_current_view') && boolean(params['capture_current_view'], 'capture_current_view')
            raise "SketchUp could not update scene: #{page.name}" unless page.update(PAGE_USE_ALL)
          end
          Verification.check_scene!('scenes.update', checks, model, expected_name, expected_description)
        end
        { 'scene' => scene_summary(page) }
          .merge(Verification.verified('scenes.update', checks))
      end

      def remove_scene(params)
        require_keys(params, %w[name], %w[name])
        page = find_scene(params['name'])
        name = page.name.to_s
        checks = []
        with_operation('DCC-MCP Remove Scene') do
          raise "SketchUp could not remove scene: #{name}" unless model.pages.erase(page)

          Verification.check_scene_absent!('scenes.remove', checks, model, name)
        end
        { 'removed_scene' => name }
          .merge(Verification.verified('scenes.remove', checks))
      end

      def list_tags(params)
        require_keys(params, [])
        values = model.layers.map { |layer| tag_summary(layer) }
        { 'tags' => values, 'total' => values.length, 'active_tag' => model.active_layer.name.to_s }
      end

      def create_tag(params)
        require_keys(params, %w[name visible], %w[name])
        name = non_empty_string(params['name'], 'name')
        raise ArgumentError, "tag already exists: #{name}" if find_tag(name, false)

        created = nil
        checks = []
        expected_visible = boolean(params['visible'], 'visible') if params.key?('visible')
        with_operation('DCC-MCP Create Tag') do
          created = model.layers.add(name)
          created.visible = boolean(params['visible'], 'visible') if params.key?('visible')
          Verification.check_tag!('tags.create', checks, model, name, expected_visible)
        end
        { 'tag' => tag_summary(created) }
          .merge(Verification.verified('tags.create', checks))
      end

      def assign_tag(params)
        require_keys(params, %w[entity_id tag], %w[entity_id tag])
        item = entity(params['entity_id'])
        tag = find_tag(params['tag'])
        raise ArgumentError, 'entity does not support tags' unless item.respond_to?(:layer=)

        checks = []
        with_operation('DCC-MCP Assign Tag') do
          item.layer = tag
          Verification.check_assigned_tag!('tags.assign', checks, item, tag)
        end
        { 'entity' => entity_summary(item), 'tag' => tag_summary(tag) }
          .merge(Verification.verified('tags.assign', checks))
      end

      def remove_tag(params)
        require_keys(params, %w[name], %w[name])
        tag = find_tag(params['name'])
        raise ArgumentError, 'the default Untagged tag cannot be removed' if tag == model.layers[0]
        raise ArgumentError, "tag is in use: #{tag.name}" if tag_in_use?(tag)

        name = tag.name.to_s
        checks = []
        with_operation('DCC-MCP Remove Tag') do
          raise "SketchUp could not remove tag: #{name}" unless model.layers.remove(tag)

          Verification.check_tag_absent!('tags.remove', checks, model, name)
        end
        { 'removed_tag' => name }
          .merge(Verification.verified('tags.remove', checks))
      end

      def model
        Sketchup.active_model || raise('No active SketchUp model')
      end

      def with_operation(name)
        started = model.start_operation(name, true)
        raise "SketchUp could not start operation: #{name}" unless started

        result = yield
        raise "SketchUp could not commit operation: #{name}" unless model.commit_operation

        started = false
        result
      rescue StandardError
        model.abort_operation if started
        raise
      end

      def entity(value)
        id = integer(value, 'entity_id', 1)
        found = model.find_entity_by_persistent_id(id)
        found = found.first if found.is_a?(Array)
        raise ArgumentError, "entity was not found: #{id}" unless found&.valid?

        found
      end

      def entities(values)
        entity_id_array(values).map { |value| entity(value) }
      end

      def root_entity(value)
        found = entity(value)
        raise ArgumentError, 'entity must be in the model root context' unless model.entities.to_a.include?(found)

        found
      end

      def root_entities(values)
        entity_id_array(values).map { |value| root_entity(value) }
      end

      def entity_summary(item)
        summary = {
          'persistent_id' => persistent_id(item),
          'entity_id' => item.entityID,
          'type' => item.typename.to_s,
          'valid' => item.valid?,
          'hidden' => item.respond_to?(:hidden?) ? item.hidden? : false,
          'locked' => item.respond_to?(:locked?) ? item.locked? : false
        }
        summary['name'] = item.name.to_s if item.respond_to?(:name)
        summary['tag'] = item.layer.name.to_s if item.respond_to?(:layer) && item.layer
        if item.respond_to?(:material)
          summary['material'] = item.material ? item.material.display_name.to_s : nil
        end
        summary['bounds'] = bounds_summary(item.bounds) if item.respond_to?(:bounds)
        summary
      end

      def persistent_id(item)
        raise 'entity does not expose a persistent id' unless item.respond_to?(:persistent_id)

        item.persistent_id
      end

      def bounds_summary(bounds)
        {
          'empty' => bounds.empty?,
          'min' => point_array(bounds.min),
          'max' => point_array(bounds.max),
          'width' => bounds.width.to_f,
          'height' => bounds.height.to_f,
          'depth' => bounds.depth.to_f,
          'unit' => 'inches'
        }
      end

      def material_summary(material)
        color = material.color
        {
          'name' => material.name.to_s,
          'display_name' => material.display_name.to_s,
          'color' => [color.red, color.green, color.blue],
          'alpha' => material.alpha.to_f,
          'texture_path' => material.texture ? material.texture.filename.to_s : nil
        }
      end

      def apply_material_values(material, params)
        if params.key?('color')
          values = color_values(params['color'])
          material.color = Sketchup::Color.new(*values)
        end
        material.alpha = number(params['alpha'], 'alpha', 0, 1) if params.key?('alpha')
        if params.key?('texture_path')
          material.texture = input_file(params['texture_path'], 'texture_path', %w[.bmp .jpg .jpeg .png .tif .tiff])
        end
      end

      def find_material(value, required = true)
        name = non_empty_string(value, 'material')
        found = model.materials.find { |material| material.name.to_s == name || material.display_name.to_s == name }
        raise ArgumentError, "material was not found: #{name}" if required && !found

        found
      end

      def material_in_use?(material)
        each_drawing_element.any? do |item|
          (item.respond_to?(:material) && item.material == material) ||
            (item.respond_to?(:back_material) && item.back_material == material)
        end
      end

      def scene_summary(page)
        {
          'name' => page.name.to_s,
          'description' => page.description.to_s,
          'include_in_animation' => page.respond_to?(:include_in_animation?) ? page.include_in_animation? : true,
          'delay_time' => page.respond_to?(:delay_time) ? page.delay_time.to_f : nil,
          'transition_time' => page.respond_to?(:transition_time) ? page.transition_time.to_f : nil
        }
      end

      def find_scene(value, required = true)
        name = non_empty_string(value, 'scene')
        found = model.pages.find { |page| page.name.to_s == name }
        raise ArgumentError, "scene was not found: #{name}" if required && !found

        found
      end

      def tag_summary(tag)
        {
          'name' => tag.name.to_s,
          'visible' => tag.visible?,
          'active' => tag == model.active_layer,
          'default' => tag == model.layers[0]
        }
      end

      def find_tag(value, required = true)
        name = non_empty_string(value, 'tag')
        found = model.layers.find { |tag| tag.name.to_s == name }
        raise ArgumentError, "tag was not found: #{name}" if required && !found

        found
      end

      def tag_in_use?(tag)
        each_drawing_element.any? { |item| item.respond_to?(:layer) && item.layer == tag }
      end

      def each_drawing_element
        values = []
        model.entities.each { |entity| values << entity }
        model.definitions.each do |definition|
          definition.entities.each { |entity| values << entity }
        end
        values
      end

      def probe_entry(entry)
        text = entry.to_s
        unless PROBE_ENTRY_PATTERN.match?(text)
          raise ArgumentError, "probe entry must be Owner#symbol or Owner.symbol: #{entry.inspect}"
        end

        owner_name, symbol = text.split(/#(?!.*#)|\.(?!.*\.)/)
        owner = resolve_probe_owner(owner_name)
        {
          'owner' => owner_name,
          'symbol' => symbol,
          'present' => probe_present?(owner, symbol)
        }
      end

      def resolve_probe_owner(owner_name)
        owner_name.to_s.split('::').reduce(Object) do |namespace, segment|
          return nil unless namespace.const_defined?(segment, false)

          namespace.const_get(segment, false)
        end
      rescue NameError
        nil
      end

      # A symbol is present when the owner answers to it either as an instance
      # method or as a singleton method. `#alpha=` style setters are instance
      # methods, `Sketchup.version` style accessors are singleton methods.
      def probe_present?(owner, symbol)
        return false if owner.nil?

        name = symbol.to_sym
        owner.instance_methods.include?(name) ||
          owner.private_instance_methods.include?(name) ||
          owner.respond_to?(name)
      rescue StandardError
        false
      end

      def require_keys(params, allowed, required = [])
        unexpected = params.keys - allowed
        raise ArgumentError, "Unexpected parameters: #{unexpected.sort.join(', ')}" unless unexpected.empty?

        missing = required - params.keys
        raise ArgumentError, "Missing required parameters: #{missing.sort.join(', ')}" unless missing.empty?
      end

      def non_empty_string(value, name)
        raise ArgumentError, "#{name} must be a string" unless value.is_a?(String)

        text = value.strip
        raise ArgumentError, "#{name} must be a non-empty string" if text.empty?

        text
      end

      def optional_string(value)
        return nil if value.nil?
        raise ArgumentError, 'value must be a string' unless value.is_a?(String)

        text = value.strip
        text.empty? ? nil : text
      end

      def boolean(value, name)
        raise ArgumentError, "#{name} must be a boolean" unless value == true || value == false

        value
      end

      def number(value, name, minimum = nil, maximum = nil)
        raise ArgumentError, "#{name} must be numeric" unless value.is_a?(Numeric) && !value.is_a?(Complex)

        result = value.to_f
        raise ArgumentError, "#{name} must be finite" unless result.finite?
        raise ArgumentError, "#{name} must be at least #{minimum}" if minimum && result < minimum
        raise ArgumentError, "#{name} must be at most #{maximum}" if maximum && result > maximum

        result
      end

      def integer(value, name, minimum = nil, maximum = nil)
        raise ArgumentError, "#{name} must be an integer" unless value.is_a?(Integer)
        raise ArgumentError, "#{name} must be at least #{minimum}" if minimum && value < minimum
        raise ArgumentError, "#{name} must be at most #{maximum}" if maximum && value > maximum

        value
      end

      def non_empty_array(value, name)
        raise ArgumentError, "#{name} must be a non-empty array" unless value.is_a?(Array) && !value.empty?

        value
      end

      def entity_id_array(value)
        values = non_empty_array(value, 'entity_ids')
        raise ArgumentError, 'entity_ids must contain at most 500 values' if values.length > 500

        ids = values.map { |item| integer(item, 'entity_ids', 1) }
        raise ArgumentError, 'entity_ids must be unique' unless ids.uniq.length == ids.length

        ids
      end

      def numeric_triplet(value, name)
        raise ArgumentError, "#{name} must contain three numbers" unless value.is_a?(Array) && value.length == 3

        value.map { |item| number(item, name) }
      end

      def scale_triplet(value)
        values = numeric_triplet(value, 'scale')
        raise ArgumentError, 'scale values must be greater than zero' unless values.all?(&:positive?)

        values
      end

      def color_values(value)
        raise ArgumentError, 'color must contain three integer channels' unless value.is_a?(Array) && value.length == 3

        value.map { |item| integer(item, 'color', 0, 255) }
      end

      def unit_name(value)
        name = value.to_s.downcase
        raise ArgumentError, "unit must be one of: #{UNIT_FACTORS.keys.join(', ')}" unless UNIT_FACTORS.key?(name)

        name
      end

      def positive_length(value, name, unit)
        number(value, name, 0.000_001) * UNIT_FACTORS.fetch(unit)
      end

      def point(value, name, unit)
        Geom::Point3d.new(*numeric_triplet(value, name).map { |item| item * UNIT_FACTORS.fetch(unit) })
      end

      def vector(value, name, unit)
        numeric_triplet(value, name).map { |item| item * UNIT_FACTORS.fetch(unit) }
      end

      def offset_point(origin, x, y, z)
        Geom::Point3d.new(origin.x + x, origin.y + y, origin.z + z)
      end

      def point_array(value)
        [value.x.to_f, value.y.to_f, value.z.to_f]
      end

      def current_model_path
        path = model.path.to_s
        path.empty? ? nil : File.expand_path(path)
      end

      def same_path?(left, right)
        return false if left.nil? || right.nil?
        return File.identical?(left, right) if File.exist?(left) && File.exist?(right)

        File::ALT_SEPARATOR == '\\' ? left.casecmp?(right) : left == right
      rescue SystemCallError
        File::ALT_SEPARATOR == '\\' ? left.casecmp?(right) : left == right
      end

      def input_file(value, name, extensions)
        path = absolute_path(value, name)
        raise ArgumentError, "#{name} must be an existing file" unless File.file?(path)

        validate_extension(path, name, extensions)
      end

      def output_file(value, name, extensions)
        path = absolute_path(value, name)
        raise ArgumentError, "#{name} parent directory does not exist" unless File.directory?(File.dirname(path))

        validate_extension(path, name, extensions)
      end

      def absolute_path(value, name)
        text = non_empty_string(value, name)
        raise ArgumentError, "#{name} must be an absolute path" unless Pathname.new(text).absolute?

        File.expand_path(text)
      end

      def validate_extension(path, name, extensions)
        extension = File.extname(path).downcase
        unless extensions.include?(extension)
          raise ArgumentError, "#{name} must use one of: #{extensions.sort.join(', ')}"
        end

        path
      end

      def reject_existing(path, overwrite)
        return unless File.exist?(path)
        return if overwrite == true

        raise ArgumentError, "output already exists; set overwrite=true: #{path}"
      end

      def symbol_keyed_options(value, name)
        raise ArgumentError, "#{name} must be an object" unless value.is_a?(Hash)
        raise ArgumentError, "#{name} must contain at most #{MAX_OPTION_KEYS} keys" if value.length > MAX_OPTION_KEYS
        raise ArgumentError, "#{name} keys must be strings" unless value.keys.all? { |key| key.is_a?(String) }

        value.each_with_object({}) do |(key, option_value), options|
          unless OPTION_KEY_PATTERN.match?(key)
            raise ArgumentError, "#{name} key must match #{OPTION_KEY_PATTERN.inspect}: #{key.inspect}"
          end

          options[key.to_sym] = option_value
        end
      end
    end
  end
end
