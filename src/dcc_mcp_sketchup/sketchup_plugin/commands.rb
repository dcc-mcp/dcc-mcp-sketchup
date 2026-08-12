# frozen_string_literal: true

require 'pathname'

module DccMcp
  module SketchupAdapter
    class Commands
      MAX_LIST_ITEMS = 500
      MAX_OPTION_KEYS = 64
      OPTION_KEY_PATTERN = /\A[a-z][a-z0-9_]{0,63}\z/.freeze
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
          'host_pid' => Process.pid,
          'host_thread_id' => Thread.current.object_id,
          'command_count' => @commands.length,
          'model_valid' => model.valid?
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

        { 'path' => model.path.to_s, 'saved' => true, 'modified' => model.modified? }
      end

      def save_copy(params)
        require_keys(params, %w[path overwrite], %w[path])
        overwrite = params.key?('overwrite') ? boolean(params['overwrite'], 'overwrite') : false
        path = output_file(params['path'], 'path', ['.skp'])
        reject_existing(path, overwrite)
        result = model.save_copy(path)
        raise 'SketchUp did not save the model copy' unless result

        { 'path' => path, 'saved' => true, 'current_model_path' => model.path.to_s }
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
        result = options.empty? ? model.import(path) : model.import(path, options)
        raise "SketchUp import failed: #{path}" unless result

        { 'path' => path, 'imported' => true, 'root_entity_count' => model.entities.length }
      end

      def export_model(params)
        require_keys(params, %w[path options overwrite], %w[path])
        overwrite = params.key?('overwrite') ? boolean(params['overwrite'], 'overwrite') : false
        path = output_file(params['path'], 'path', EXPORT_EXTENSIONS)
        reject_existing(path, overwrite)
        options = symbol_keyed_options(params.fetch('options', {}), 'options')
        result = options.empty? ? model.export(path) : model.export(path, options)
        raise "SketchUp export failed: #{path}" unless result

        { 'path' => path, 'exported' => true, 'exists' => File.file?(path) }
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
        end
        { 'entity' => entity_summary(created), 'unit' => unit }
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
        with_operation('DCC-MCP Add Cylinder') do
          group = model.entities.add_group
          group.name = name
          edges = group.entities.add_circle(center, [0, 0, 1], radius, segments)
          face = group.entities.add_face(edges)
          raise 'SketchUp could not create the cylinder base face' unless face

          face.reverse! if face.normal.z.negative?
          face.pushpull(height)
          created = group
        end
        { 'entity' => entity_summary(created), 'segments' => segments, 'unit' => unit }
      end

      def group_entities(params)
        require_keys(params, %w[entity_ids name], %w[entity_ids])
        entities = root_entities(params['entity_ids'])
        name = optional_string(params['name']) || 'DCC-MCP Group'
        created = nil
        with_operation('DCC-MCP Group Entities') do
          created = model.entities.add_group(entities)
          created.name = name
        end
        { 'entity' => entity_summary(created), 'grouped_count' => entities.length }
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
        transformation = Geom::Transformation.translation(translation)
        transformation *= Geom::Transformation.rotation(center, axis, degrees.degrees)
        transformation *= Geom::Transformation.scaling(center, *scale)
        with_operation('DCC-MCP Transform Entity') do
          unless model.entities.transform_entities(transformation, [entity])
            raise "SketchUp could not transform entity: #{persistent_id(entity)}"
          end
        end
        { 'entity' => entity_summary(entity), 'unit' => unit }
      end

      def rename_entity(params)
        require_keys(params, %w[entity_id name], %w[entity_id name])
        entity = entity(params['entity_id'])
        raise ArgumentError, 'entity does not support names' unless entity.respond_to?(:name=)

        name = non_empty_string(params['name'], 'name')
        with_operation('DCC-MCP Rename Entity') { entity.name = name }
        { 'entity' => entity_summary(entity) }
      end

      def erase_entities(params)
        require_keys(params, %w[entity_ids], %w[entity_ids])
        entities = root_entities(params['entity_ids'])
        ids = entities.map { |item| persistent_id(item) }
        with_operation('DCC-MCP Erase Entities') { model.entities.erase_entities(entities) }
        { 'erased_entity_ids' => ids, 'erased_count' => ids.length }
      end

      def select_entities(params)
        require_keys(params, %w[entity_ids replace], %w[entity_ids])
        entities = entities(params['entity_ids'])
        replace = params.key?('replace') ? boolean(params['replace'], 'replace') : true
        model.selection.clear if replace
        model.selection.add(entities)
        {
          'selected_entity_ids' => model.selection.map { |item| persistent_id(item) },
          'selection_count' => model.selection.length
        }
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
        with_operation('DCC-MCP Create Material') do
          created = model.materials.add(name)
          apply_material_values(created, params)
        end
        { 'material' => material_summary(created) }
      end

      def update_material(params)
        require_keys(
          params,
          %w[name new_name color alpha texture_path clear_texture],
          %w[name]
        )
        material = find_material(params['name'])
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
        end
        { 'material' => material_summary(material) }
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

        with_operation('DCC-MCP Assign Material') do
          item.material = material if %w[front both].include?(side)
          item.back_material = material if %w[back both].include?(side)
        end
        { 'entity' => entity_summary(item), 'material' => material_summary(material), 'side' => side }
      end

      def remove_material(params)
        require_keys(params, %w[name], %w[name])
        material = find_material(params['name'])
        raise ArgumentError, "material is in use: #{material.name}" if material_in_use?(material)

        name = material.name.to_s
        with_operation('DCC-MCP Remove Material') do
          raise "SketchUp could not remove material: #{name}" unless model.materials.remove(material)
        end
        { 'removed_material' => name }
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
        with_operation('DCC-MCP Create Scene') do
          created = model.pages.add(name)
          created.description = params['description'].to_s if params.key?('description')
          if params.key?('include_in_animation') && created.respond_to?(:include_in_animation=)
            created.include_in_animation = boolean(params['include_in_animation'], 'include_in_animation')
          end
        end
        { 'scene' => scene_summary(created) }
      end

      def update_scene(params)
        require_keys(
          params,
          %w[name new_name description include_in_animation capture_current_view],
          %w[name]
        )
        page = find_scene(params['name'])
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
        end
        { 'scene' => scene_summary(page) }
      end

      def remove_scene(params)
        require_keys(params, %w[name], %w[name])
        page = find_scene(params['name'])
        name = page.name.to_s
        with_operation('DCC-MCP Remove Scene') do
          raise "SketchUp could not remove scene: #{name}" unless model.pages.erase(page)
        end
        { 'removed_scene' => name }
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
        with_operation('DCC-MCP Create Tag') do
          created = model.layers.add(name)
          created.visible = boolean(params['visible'], 'visible') if params.key?('visible')
        end
        { 'tag' => tag_summary(created) }
      end

      def assign_tag(params)
        require_keys(params, %w[entity_id tag], %w[entity_id tag])
        item = entity(params['entity_id'])
        tag = find_tag(params['tag'])
        raise ArgumentError, 'entity does not support tags' unless item.respond_to?(:layer=)

        with_operation('DCC-MCP Assign Tag') { item.layer = tag }
        { 'entity' => entity_summary(item), 'tag' => tag_summary(tag) }
      end

      def remove_tag(params)
        require_keys(params, %w[name], %w[name])
        tag = find_tag(params['name'])
        raise ArgumentError, 'the default Untagged tag cannot be removed' if tag == model.layers[0]
        raise ArgumentError, "tag is in use: #{tag.name}" if tag_in_use?(tag)

        name = tag.name.to_s
        with_operation('DCC-MCP Remove Tag') do
          raise "SketchUp could not remove tag: #{name}" unless model.layers.remove(tag)
        end
        { 'removed_tag' => name }
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
