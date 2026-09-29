# frozen_string_literal: true

require 'minitest/autorun'
require 'tmpdir'
require_relative 'sketchup_fakes'
require_relative '../../src/dcc_mcp_sketchup/sketchup_plugin/commands'

# Contract tests for the bounded Ruby command map.
#
# These run against tests/ruby/sketchup_fakes.rb, an in-memory stub of the
# SketchUp API. That makes them contract evidence, not host evidence: they prove
# the command layer honours its contract (parameter validation, one undo
# operation per mutation, and a post-write read-back that disagrees loudly), not
# that a real SketchUp executed the commands. See compat_matrix.json, which
# records that bound explicitly.
class CommandsTest < Minitest::Test
  def setup
    FakeIds.reset!
    Sketchup.active_model = FakeModel.new
    @commands = DccMcp::SketchupAdapter::Commands.new
  end

  def model
    Sketchup.active_model
  end

  def box_params(overrides = {})
    {
      'width' => 2.0, 'depth' => 1.0, 'height' => 0.5,
      'unit' => 'meters', 'name' => 'RubySmoke'
    }.merge(overrides)
  end

  # --- existing contract -------------------------------------------------

  def test_ping_reports_bounded_command_map
    result = @commands.execute('diagnostics.ping', {})

    assert_equal 'ok', result['status']
    assert_equal '2026.0', result['sketchup_version']
    assert_equal RUBY_VERSION, result['ruby_version']
    assert_equal 29, result['command_count']
    assert_equal Process.pid, result['host_pid']
    assert_equal DccMcp::SketchupAdapter::Commands::ADAPTER_VERSION, result['adapter_version']
    assert_equal File.expand_path('../../src/dcc_mcp_sketchup/sketchup_plugin', __dir__), result['plugin_path']
  end

  def test_adapter_version_matches_python_package_version
    version_file = File.expand_path('../../src/dcc_mcp_sketchup/__version__.py', __dir__)
    package_version = File.read(version_file).match(/__version__ = ["']([^"']+)["']/)[1]

    assert_equal package_version, DccMcp::SketchupAdapter::Commands::ADAPTER_VERSION
  end

  def test_add_box_uses_one_undo_operation_and_returns_persistent_id
    result = @commands.execute('geometry.add_box', box_params)

    assert_equal 101, result.dig('entity', 'persistent_id')
    assert_equal 'RubySmoke', model.entities.to_a.first.name
    assert_in_delta 19.685, model.entities.to_a.first.entities.face.pushed, 0.001
    assert_equal 1, model.commits
    assert_equal 0, model.aborts
  end

  def test_expired_request_is_rejected_before_host_access
    error = assert_raises(RuntimeError) do
      @commands.execute(
        'model.inspect',
        '_dcc_mcp_deadline_unix_ms' => ((Time.now.to_f - 1) * 1000).to_i
      )
    end

    assert_match(/expired/, error.message)
  end

  def test_unknown_command_is_rejected
    error = assert_raises(ArgumentError) { @commands.execute('ruby.eval', {}) }

    assert_match(/Unsupported SketchUp command/, error.message)
  end

  def test_import_converts_bounded_json_option_keys_to_symbols
    Dir.mktmpdir do |directory|
      path = File.join(directory, 'source.dae')
      File.write(path, '<COLLADA/>')

      @commands.execute(
        'model.import',
        'path' => path,
        'options' => { 'units' => 'model', 'merge_coplanar_faces' => true }
      )

      assert_equal path, model.import_call[0]
      assert_equal({ units: 'model', merge_coplanar_faces: true }, model.import_call[1])
    end
  end

  def test_export_accepts_current_official_format_and_symbolizes_options
    Dir.mktmpdir do |directory|
      path = File.join(directory, 'scene.glb')

      @commands.execute('model.export', 'path' => path, 'options' => { 'selectionset_only' => false })

      assert_equal path, model.export_call[0]
      assert_equal({ selectionset_only: false }, model.export_call[1])
    end
  end

  def test_import_rejects_unbounded_or_invalid_option_keys
    Dir.mktmpdir do |directory|
      path = File.join(directory, 'source.dae')
      File.write(path, '<COLLADA/>')
      too_many = 65.times.to_h { |index| ["option_#{index}", true] }

      error = assert_raises(ArgumentError) do
        @commands.execute('model.import', 'path' => path, 'options' => too_many)
      end
      assert_match(/at most 64 keys/, error.message)

      error = assert_raises(ArgumentError) do
        @commands.execute('model.import', 'path' => path, 'options' => { 'not-valid!' => true })
      end
      assert_match(/must match/, error.message)
    end
  end

  def test_failed_material_removal_aborts_the_undo_operation
    model.materials.add('Surface')
    model.materials.remove_result = false

    error = assert_raises(RuntimeError) do
      @commands.execute('materials.remove', 'name' => 'Surface')
    end

    assert_match(/could not remove material/, error.message)
    assert_equal 0, model.commits
    assert_equal 1, model.aborts
  end

  def test_string_parameters_do_not_coerce_other_json_types
    error = assert_raises(ArgumentError) do
      @commands.execute('geometry.add_box', 'width' => 1, 'depth' => 1, 'height' => 1, 'name' => 42)
    end

    assert_match(/must be a string/, error.message)
  end

  # --- api probe (drives the doctor's compatibility matrix check) ---------

  def test_api_probe_reports_presence_for_each_requested_symbol
    result = @commands.execute(
      'diagnostics.api_probe',
      'symbols' => ['Sketchup::Color#red', 'Sketchup::Nonexistent#method']
    )

    probe = result['probe']
    assert_equal 2, probe.length
    assert_equal true, probe[0]['present']
    assert_equal 'Sketchup::Color', probe[0]['owner']
    assert_equal 'red', probe[0]['symbol']
    assert_equal false, probe[1]['present']
    assert_equal RUBY_VERSION, result['ruby_version']
  end

  def test_api_probe_rejects_malformed_symbols
    error = assert_raises(ArgumentError) do
      @commands.execute('diagnostics.api_probe', 'symbols' => ['not a symbol'])
    end

    assert_match(/Owner#symbol/, error.message)
  end

  def test_api_probe_requires_symbols
    assert_raises(ArgumentError) { @commands.execute('diagnostics.api_probe', {}) }
  end

  # --- post-write read-back ----------------------------------------------

  def test_add_box_returns_verified_extents
    result = @commands.execute('geometry.add_box', box_params)
    verification = result['verification']

    assert_equal true, verification['verified']
    assert_equal 'geometry.add_box', verification['tool']
    assert_equal '2026.0', verification['host_version']
    names = verification['checks'].map { |check| check['check'] }
    assert_includes names, 'entity_present'
    assert_includes names, 'entity_name'
    assert_includes names, 'bounds_extents'

    # 2m x 1m x 0.5m expressed in inches, which is the unit bounds report.
    extents = verification['checks'].find { |check| check['check'] == 'bounds_extents' }
    assert_in_delta 78.740, extents['expected'][0], 0.001
    assert_in_delta 39.370, extents['expected'][1], 0.001
    assert_in_delta 19.685, extents['expected'][2], 0.001
    assert_equal extents['expected'], extents['actual']
  end

  def test_add_box_rejects_geometry_that_does_not_match_the_request
    # Simulate a pushpull that returned without producing the requested volume:
    # every newly created group reports a degenerate 1x1x1 bounding box.
    original = FakeGroup.instance_method(:apply_extents)
    FakeGroup.define_method(:apply_extents) do |_points, _height|
      @bounds = FakeBounds.new(1, 1, 1)
    end

    error = assert_raises(DccMcp::SketchupAdapter::VerificationFailure) do
      @commands.execute('geometry.add_box', box_params)
    end

    assert_match(/bounds_extents/, error.message)
    assert_match(/host SketchUp 2026.0/, error.message)
    assert_equal 'bounds_extents', error.payload['check']
    assert_equal 'geometry.add_box', error.payload['tool']
    assert_equal '2026.0', error.payload['host_version']
    assert_equal DccMcp::SketchupAdapter::Verification::ERROR_CODE, error.payload['code']

    # The read-back runs inside the undo operation, so a disagreement aborts it
    # instead of committing geometry that does not match the request.
    assert_equal 0, model.commits
    assert_equal 1, model.aborts
  ensure
    FakeGroup.define_method(:apply_extents, original)
  end

  def test_add_cylinder_returns_verified_diameter_and_height
    result = @commands.execute(
      'geometry.add_cylinder',
      'radius' => 0.5, 'height' => 2.0, 'unit' => 'meters'
    )
    extents = result['verification']['checks'].find { |check| check['check'] == 'bounds_extents' }

    assert_in_delta 39.370, extents['expected'][0], 0.001
    assert_in_delta 39.370, extents['expected'][1], 0.001
    assert_in_delta 78.740, extents['expected'][2], 0.001
    assert_equal extents['expected'], extents['actual']
  end

  def test_group_entities_verifies_the_members_were_moved_into_the_group
    first = model.entities.add_group
    second = model.entities.add_group

    result = @commands.execute('geometry.group', 'entity_ids' => [first.persistent_id, second.persistent_id])
    members = result['verification']['checks'].find { |check| check['check'] == 'group_member_count' }

    assert_equal 2, members['expected']
    assert_equal 2, members['actual']
  end

  def test_rename_entity_verifies_the_new_name
    group = model.entities.add_group

    result = @commands.execute('entity.rename', 'entity_id' => group.persistent_id, 'name' => 'Wall')
    rename = result['verification']['checks'].find { |check| check['check'] == 'entity_name' }

    assert_equal 'Wall', rename['expected']
    assert_equal 'Wall', rename['actual']
  end

  def test_rename_entity_rejects_a_name_that_did_not_stick
    group = model.entities.add_group
    # Simulate a host that accepted the setter but did not apply it.
    group.define_singleton_method(:name) { 'Stale' }

    error = assert_raises(DccMcp::SketchupAdapter::VerificationFailure) do
      @commands.execute('entity.rename', 'entity_id' => group.persistent_id, 'name' => 'Wall')
    end

    assert_equal 'entity_name', error.payload['check']
    assert_equal 'Wall', error.payload['expected']
    assert_equal 'Stale', error.payload['actual']
  end

  def test_erase_entities_verifies_nothing_resolves_afterwards
    group = model.entities.add_group

    result = @commands.execute('entity.erase', 'entity_ids' => [group.persistent_id])
    absent = result['verification']['checks'].find { |check| check['check'] == 'entity_absent' }

    assert_equal [], absent['expected']
    assert_equal [], absent['actual']
    assert_equal 0, model.entities.length
  end

  def test_select_entities_verifies_the_selection
    group = model.entities.add_group

    result = @commands.execute('entity.select', 'entity_ids' => [group.persistent_id])
    selected = result['verification']['checks'].find { |check| check['check'] == 'selection_contains' }

    assert_equal [group.persistent_id], selected['expected']
    assert_equal [group.persistent_id], selected['actual']
  end

  def test_transform_entity_verifies_the_centre_moved_by_the_translation
    group = model.entities.add_group
    group.drift_bounds(2, 2, 2)

    result = @commands.execute(
      'entity.transform',
      'entity_id' => group.persistent_id,
      'translation' => [1, 0, 0],
      'unit' => 'meters'
    )
    centre = result['verification']['checks'].find { |check| check['check'] == 'bounds_center' }

    # The centre starts at (1, 1, 1) and the translation is 1m = 39.37 inches.
    assert_in_delta 1 + 39.370, centre['expected'][0], 0.001
    assert_equal centre['expected'], centre['actual']
  end

  def test_transform_entity_rejects_a_transform_that_did_not_move_the_entity
    group = model.entities.add_group
    group.drift_bounds(2, 2, 2)
    model.entities.define_singleton_method(:transform_entities) { |_transformation, list| list }

    error = assert_raises(DccMcp::SketchupAdapter::VerificationFailure) do
      @commands.execute(
        'entity.transform',
        'entity_id' => group.persistent_id,
        'translation' => [1, 0, 0],
        'unit' => 'meters'
      )
    end

    assert_equal 'bounds_center', error.payload['check']
    refute_equal error.payload['expected'], error.payload['actual']
  end

  def test_material_create_verifies_the_applied_values
    result = @commands.execute(
      'materials.create', 'name' => 'Brick', 'color' => [10, 20, 30], 'alpha' => 0.5
    )
    checks = result['verification']['checks'].map { |check| check['check'] }

    assert_includes checks, 'material_present'
    assert_includes checks, 'material_color'
    assert_includes checks, 'material_alpha'
    assert_equal [10, 20, 30], result['verification']['checks'].find { |c| c['check'] == 'material_color' }['actual']
    assert_in_delta 0.5, result['verification']['checks'].find { |c| c['check'] == 'material_alpha' }['actual'], 1e-9
  end

  def test_material_create_rejects_an_alpha_that_was_not_applied
    model.materials.define_singleton_method(:add) do |name|
      material = Sketchup::Material.new(name)
      material.define_singleton_method(:alpha) { 1.0 }
      @values << material
      material
    end

    error = assert_raises(DccMcp::SketchupAdapter::VerificationFailure) do
      @commands.execute('materials.create', 'name' => 'Brick', 'alpha' => 0.25)
    end

    assert_equal 'material_alpha', error.payload['check']
    assert_in_delta 0.25, error.payload['expected'], 1e-9
    assert_in_delta 1.0, error.payload['actual'], 1e-9
  end

  def test_material_update_verifies_the_renamed_target
    model.materials.add('Brick')

    result = @commands.execute('materials.update', 'name' => 'Brick', 'new_name' => 'Stone')
    present = result['verification']['checks'].find { |check| check['check'] == 'material_present' }

    assert_equal 'Stone', present['expected']
    assert_equal 'Stone', present['actual']
  end

  def test_material_remove_verifies_absence
    model.materials.add('Brick')

    result = @commands.execute('materials.remove', 'name' => 'Brick')
    absent = result['verification']['checks'].find { |check| check['check'] == 'material_absent' }

    assert_equal false, absent['expected']
    assert_equal false, absent['actual']
  end

  def test_scene_lifecycle_is_verified
    @commands.execute('scenes.create', 'name' => 'Shot 1', 'description' => 'wide')
    update = @commands.execute('scenes.update', 'name' => 'Shot 1', 'new_name' => 'Shot 2')
    assert_equal 'Shot 2', update.dig('verification', 'checks')
                                 .find { |check| check['check'] == 'scene_present' }['actual']

    removal = @commands.execute('scenes.remove', 'name' => 'Shot 2')
    absent = removal.dig('verification', 'checks').find { |check| check['check'] == 'scene_absent' }
    assert_equal false, absent['actual']
  end

  def test_scene_update_rejects_a_rename_that_did_not_apply
    model.pages.add('Shot 1')
    model.pages.define_singleton_method(:add) { |name| Sketchup::Page.new(name) }
    page = model.pages.first
    page.define_singleton_method(:name=) { |_value| nil }

    error = assert_raises(DccMcp::SketchupAdapter::VerificationFailure) do
      @commands.execute('scenes.update', 'name' => 'Shot 1', 'new_name' => 'Shot 2')
    end

    assert_equal 'scene_present', error.payload['check']
    assert_equal 'Shot 2', error.payload['expected']
    assert_nil error.payload['actual']
  end

  def test_tag_lifecycle_is_verified
    @commands.execute('tags.create', 'name' => 'Walls', 'visible' => false)
    update = @commands.execute('tags.list', {})
    assert_includes update['tags'].map { |tag| tag['name'] }, 'Walls'

    removal = @commands.execute('tags.remove', 'name' => 'Walls')
    absent = removal.dig('verification', 'checks').find { |check| check['check'] == 'tag_absent' }
    assert_equal false, absent['actual']
  end

  def test_tag_remove_rejects_a_tag_that_survived
    model.layers.add('Walls')
    model.layers.define_singleton_method(:remove) { |_layer| true }

    error = assert_raises(DccMcp::SketchupAdapter::VerificationFailure) do
      @commands.execute('tags.remove', 'name' => 'Walls')
    end

    assert_equal 'tag_absent', error.payload['check']
    assert_equal true, error.payload['actual']
  end

  def test_assign_tag_verifies_the_entity_moved
    group = model.entities.add_group
    model.layers.add('Walls')

    result = @commands.execute('tags.assign', 'entity_id' => group.persistent_id, 'tag' => 'Walls')
    assigned = result.dig('verification', 'checks').find { |check| check['check'] == 'entity_tag' }

    assert_equal 'Walls', assigned['expected']
    assert_equal 'Walls', assigned['actual']
  end

  def test_save_model_verifies_the_file_and_the_modified_flag
    Dir.mktmpdir do |directory|
      path = File.join(directory, 'model.skp')

      result = @commands.execute('model.save', 'path' => path)
      checks = result['verification']['checks'].map { |check| check['check'] }

      assert_includes checks, 'file_exists'
      assert_includes checks, 'file_non_empty'
      assert_includes checks, 'model_not_modified'
      assert File.file?(path)
    end
  end

  def test_save_copy_verifies_the_copy_exists
    Dir.mktmpdir do |directory|
      path = File.join(directory, 'copy.skp')

      result = @commands.execute('model.save_copy', 'path' => path)

      assert_equal true, result.dig('verification', 'verified')
      assert File.file?(path)
    end
  end

  def test_export_verifies_the_output_file
    Dir.mktmpdir do |directory|
      path = File.join(directory, 'scene.glb')

      result = @commands.execute('model.export', 'path' => path)

      assert_equal true, result.dig('verification', 'verified')
      assert File.file?(path)
    end
  end

  def test_export_rejects_a_successful_call_that_wrote_nothing
    Dir.mktmpdir do |directory|
      path = File.join(directory, 'scene.glb')
      model.define_singleton_method(:export) { |_target, _options = nil| true }

      error = assert_raises(DccMcp::SketchupAdapter::VerificationFailure) do
        @commands.execute('model.export', 'path' => path)
      end

      assert_equal 'file_exists', error.payload['check']
      assert_equal true, error.payload['expected']
      assert_equal false, error.payload['actual']
    end
  end

  def test_import_verifies_geometry_was_not_lost
    Dir.mktmpdir do |directory|
      path = File.join(directory, 'source.dae')
      File.write(path, '<COLLADA/>')

      result = @commands.execute('model.import', 'path' => path)
      checks = result['verification']['checks'].map { |check| check['check'] }

      assert_includes checks, 'root_entity_count_not_decreased'
      assert_includes checks, 'source_file_readable'
    end
  end

  # --- the contract itself -----------------------------------------------

  def test_every_mutating_command_returns_a_verified_block
    mutating = %w[
      model.save_copy model.import model.export
      geometry.add_box geometry.add_cylinder geometry.group
      entity.rename entity.erase entity.select
      materials.create materials.remove materials.assign
      scenes.create scenes.remove
      tags.create tags.remove
    ]

    Dir.mktmpdir do |directory|
      # model.save_copy / export need a writable target; import needs a source.
      File.write(File.join(directory, 'source.dae'), '<COLLADA/>')
      model.entities.add_group
      model.materials.add('Brick')
      model.pages.add('Shot 1')
      model.layers.add('Walls')

      mutating.each do |name|
        params = case name
                 when 'model.save_copy' then { 'path' => File.join(directory, 'copy.skp') }
                 when 'model.export' then { 'path' => File.join(directory, 'scene.glb') }
                 when 'model.import' then { 'path' => File.join(directory, 'source.dae') }
                 when 'geometry.add_box' then box_params
                 when 'geometry.add_cylinder' then { 'radius' => 0.5, 'height' => 2.0 }
                 when 'geometry.group' then { 'entity_ids' => [model.entities.first.persistent_id] }
                 when 'entity.rename' then { 'entity_id' => model.entities.first.persistent_id, 'name' => 'Wall' }
                 when 'entity.erase', 'entity.select' then { 'entity_ids' => [model.entities.first.persistent_id] }
                 when 'materials.create' then { 'name' => 'Stone' }
                 when 'materials.remove' then { 'name' => 'Stone' }
                 when 'materials.assign' then { 'entity_id' => model.entities.first.persistent_id, 'material' => 'Brick' }
                 when 'scenes.create' then { 'name' => 'Shot 2' }
                 when 'scenes.remove' then { 'name' => 'Shot 1' }
                 when 'tags.create' then { 'name' => 'Doors' }
                 when 'tags.remove' then { 'name' => 'Walls' }
                 else {}
                 end

        result = @commands.execute(name, params)
        assert_equal true, result.dig('verification', 'verified'), "#{name} returned no verified read-back"
        refute_empty result.dig('verification', 'checks'), "#{name} verified with no checks"
        assert_equal name, result.dig('verification', 'tool')
      end
    end
  end

  def test_read_only_commands_carry_no_verification_block
    %w[diagnostics.ping model.inspect model.list_entities model.validate
       materials.list scenes.list tags.list].each do |name|
      result = @commands.execute(name, {})

      refute_includes result.keys, 'verification'
    end
  end
end
