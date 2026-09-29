# frozen_string_literal: true

require 'minitest/autorun'
require 'tmpdir'
require_relative 'sketchup_fakes'
require_relative '../../src/dcc_mcp_sketchup/sketchup_plugin/verification'

# Unit tests for the post-write read-back helpers.
#
# These pin the two properties the read-back contract depends on, and both are
# about what the caller can act on rather than about which comparison won:
#
#   * expected and actual are always both reported, and
#   * the host version is always attached.
#
# A mismatch that only said "failed" would make the caller guess, and a report
# without the host version is unreproducible -- a read-back that disagrees is
# the classic signature of host API drift.
class VerificationTest < Minitest::Test
  def setup
    FakeIds.reset!
    Sketchup.active_model = FakeModel.new
    @checks = []
  end

  def verification
    DccMcp::SketchupAdapter::Verification
  end

  # --- comparison semantics ----------------------------------------------

  def test_numbers_match_within_tolerance
    assert verification.matches?(1.0, 1.0)
    assert verification.matches?(1.0, 1.0 + 1e-12)
    refute verification.matches?(1.0, 1.001)
  end

  def test_sequence_mismatch_includes_length_differences
    # A length mismatch is a mismatch, never a truncated comparison: reporting
    # three coordinates against two would hide the difference.
    assert verification.matches?([1, 2, 3], [1, 2, 3])
    refute verification.matches?([1, 2, 3], [1, 2])
    refute verification.matches?([1, 2], [1, 2, 3])
  end

  def test_type_mismatch_is_a_mismatch
    refute verification.matches?(1, '1')
    refute verification.matches?([1, 2], ['1', '2'])
    refute verification.matches?({ 'a' => 1 }, { 'a' => 2 })
  end

  def test_nil_never_matches_a_value
    refute verification.matches?('Brick', nil)
    assert verification.matches?(nil, nil)
  end

  # --- evidence and failure shape ----------------------------------------

  def test_check_appends_before_comparing
    verification.check!('tool.name', @checks, 'a_check', 1, 1)

    assert_equal 1, @checks.length
    assert_equal 'a_check', @checks[0]['check']
    assert_equal 1, @checks[0]['expected']
    assert_equal 1, @checks[0]['actual']
  end

  def test_failed_check_is_still_recorded_in_the_evidence_trail
    # The failing entry is appended before the comparison, so it is part of the
    # trail even though it never reaches the caller inside a verified block.
    assert_raises(DccMcp::SketchupAdapter::VerificationFailure) do
      verification.check!('tool.name', @checks, 'a_check', 1, 2)
    end

    assert_equal 1, @checks.length
    assert_equal 1, @checks[0]['expected']
    assert_equal 2, @checks[0]['actual']
  end

  def test_failure_payload_is_json_shaped_and_carries_the_host_version
    error = assert_raises(DccMcp::SketchupAdapter::VerificationFailure) do
      verification.check!('tool.name', @checks, 'a_check', [1, 2], [3, 4])
    end

    payload = error.payload
    assert_equal 'tool.name', payload['tool']
    assert_equal 'a_check', payload['check']
    assert_equal [1, 2], payload['expected']
    assert_equal [3, 4], payload['actual']
    assert_equal '2026.0', payload['host_version']
    assert_equal verification::ERROR_CODE, payload['code']
    assert_equal 1, payload['schema_version']
    # Both values, named, in one sentence: the reader should never have to
    # re-run the call to find out what differed.
    assert_match(/expected \[1, 2\]/, error.message)
    assert_match(/read back \[3, 4\]/, error.message)
    assert_match(/host SketchUp 2026\.0/, error.message)
  end

  def test_verified_block_shape
    verification.check!('tool.name', @checks, 'a_check', 'x', 'x')
    block = verification.verified('tool.name', @checks)

    assert_equal true, block['verification']['verified']
    assert_equal 'tool.name', block['verification']['tool']
    assert_equal '2026.0', block['verification']['host_version']
    assert_equal 1, block['verification']['check_count']
    assert_equal @checks, block['verification']['checks']
  end

  def test_error_code_matches_the_python_contract
    # bridge.py keys off this exact string to rebuild WriteVerificationError,
    # so a rename here would silently downgrade a structured mismatch to prose.
    assert_equal 'write_verification_failed', verification::ERROR_CODE
  end

  # --- read-back helpers --------------------------------------------------

  def test_entity_present_reports_nil_when_the_entity_is_gone
    group = Sketchup.active_model.entities.add_group
    identifier = group.persistent_id
    found = verification.check_entity_present!('tool', @checks, Sketchup.active_model, identifier)
    assert_equal group, found

    Sketchup.active_model.entities.erase_entities([group])
    assert_raises(DccMcp::SketchupAdapter::VerificationFailure) do
      verification.check_entity_present!('tool', @checks, Sketchup.active_model, identifier)
    end
    assert_equal identifier, @checks[1]['expected']
    assert_nil @checks[1]['actual']
  end

  def test_extents_check_rejects_a_degenerate_bounding_box
    group = Sketchup.active_model.entities.add_group
    group.drift_bounds(1, 1, 1)

    assert_raises(DccMcp::SketchupAdapter::VerificationFailure) do
      verification.check_extents!('tool', @checks, group, [2, 2, 2])
    end
  end

  def test_absent_check_reports_every_surviving_entity
    first = Sketchup.active_model.entities.add_group
    second = Sketchup.active_model.entities.add_group
    Sketchup.active_model.entities.erase_entities([second])

    assert_raises(DccMcp::SketchupAdapter::VerificationFailure) do
      verification.check_absent!(
        'tool', @checks, Sketchup.active_model,
        [first.persistent_id, second.persistent_id], 'entity'
      )
    end

    assert_equal [], @checks[0]['expected']
    assert_equal [first.persistent_id], @checks[0]['actual']
  end

  def test_center_moved_check_uses_the_translation_only
    group = Sketchup.active_model.entities.add_group
    group.drift_bounds(2, 2, 2)
    # The centre starts at (1, 1, 1) for a 2x2x2 box at the origin.
    group.translate([1, 0, 0])

    verification.check_center_moved!('tool', @checks, group, [1, 1, 1], [1, 0, 0])

    # Rotation and scaling are applied about the centre and leave it invariant,
    # so translation is the only component the centre must reflect.
    assert_equal [2.0, 1.0, 1.0], @checks[0]['expected']
    assert_equal [2.0, 1.0, 1.0], @checks[0]['actual']
  end

  def test_count_not_decreased_allows_an_increase
    # An import that adds nothing to the root context is legitimate; losing
    # geometry is not. So the check is an invariant, not an equality.
    verification.check_count_not_decreased!('tool', @checks, 2, 5)
    verification.check_count_not_decreased!('tool', @checks, 2, 2)

    assert_raises(DccMcp::SketchupAdapter::VerificationFailure) do
      verification.check_count_not_decreased!('tool', @checks, 2, 1)
    end
    assert_equal 2, @checks[2]['expected']
    assert_equal 1, @checks[2]['actual']
  end

  def test_file_written_check_covers_existence_and_size
    Dir.mktmpdir do |directory|
      path = File.join(directory, 'out.glb')
      File.write(path, 'payload')

      verification.check_file_written!('tool', @checks, path)

      assert_equal %w[file_exists file_non_empty], @checks.map { |check| check['check'] }
      assert_equal [true, true], @checks.map { |check| check['actual'] }
    end
  end

  def test_file_written_check_fails_on_a_missing_file
    Dir.mktmpdir do |directory|
      assert_raises(DccMcp::SketchupAdapter::VerificationFailure) do
        verification.check_file_written!('tool', @checks, File.join(directory, 'absent.glb'))
      end

      assert_equal 'file_exists', @checks[0]['check']
      assert_equal false, @checks[0]['actual']
    end
  end

  def test_file_written_check_fails_on_an_empty_file
    Dir.mktmpdir do |directory|
      path = File.join(directory, 'empty.glb')
      File.write(path, '')

      assert_raises(DccMcp::SketchupAdapter::VerificationFailure) do
        verification.check_file_written!('tool', @checks, path)
      end

      assert_equal 'file_non_empty', @checks[1]['check']
      assert_equal false, @checks[1]['actual']
    end
  end

  def test_material_check_reads_back_only_the_supplied_values
    # A read-back that demanded defaults would fail on any host that fills them
    # differently, so only the fields the caller supplied are asserted.
    model = Sketchup.active_model
    model.materials.add('Brick')

    assert_raises(DccMcp::SketchupAdapter::VerificationFailure) do
      verification.check_material!('tool', @checks, model, 'Brick', 'color' => [1, 2, 3])
    end

    assert_equal %w[material_present material_color], @checks.map { |check| check['check'] }
    assert_equal [1, 2, 3], @checks[1]['expected']
    assert_equal [0, 0, 0], @checks[1]['actual']
  end

  def test_material_check_reports_a_missing_material
    assert_raises(DccMcp::SketchupAdapter::VerificationFailure) do
      verification.check_material!('tool', @checks, Sketchup.active_model, 'Nope', {})
    end

    assert_equal 'material_present', @checks[0]['check']
    assert_equal 'Nope', @checks[0]['expected']
    assert_nil @checks[0]['actual']
  end

  def test_material_texture_check_compares_presence_not_path
    # SketchUp normalises texture paths per platform, so an exact string match
    # would fail on separator or case differences while the texture was in fact
    # applied correctly.
    model = Sketchup.active_model
    material = model.materials.add('Brick')
    material.texture = 'C:/textures/brick.png'

    verification.check_material_texture!('tool', @checks, model, 'Brick')

    assert_equal 'material_texture', @checks[0]['check']
    assert_equal true, @checks[0]['actual']
  end
end
