# frozen_string_literal: true

module DccMcp
  module SketchupAdapter
    # Raised when a mutating command's post-write read-back disagrees.
    #
    # The structured payload travels to the Python side of the bridge, so it is
    # a flat JSON-shaped Hash of primitives only. Anything richer would be lost
    # at the boundary and the report would degrade into "expected something,
    # read back something".
    class VerificationFailure < StandardError
      attr_reader :payload

      def initialize(payload)
        @payload = payload
        super(payload['message'])
      end
    end

    # Post-write read-back helpers for the bounded command map.
    #
    # SketchUp is the only runtime that can see the model, so every check here
    # runs on the Ruby side, inside the SketchUp main thread, before the undo
    # operation is committed. A failed read-back therefore rolls the mutation
    # back instead of leaving half-applied geometry behind.
    #
    # The Python side proves this evidence actually arrived; see
    # dcc_mcp_sketchup/write_contract.py.
    module Verification
      SCHEMA_VERSION = 1
      REL_TOLERANCE = 1e-6
      ABS_TOLERANCE = 1e-9

      # Must match write_contract.WRITE_VERIFICATION_CODE so that bridge.py can
      # rebuild WriteVerificationError from the payload.
      ERROR_CODE = 'write_verification_failed'

      class << self
        def host_version
          Sketchup.version.to_s
        end

        # Structural comparison with the contract's floating point tolerance.
        # Numeric, Array, and Hash are compared piecewise; everything else by
        # equality. A length mismatch is a mismatch, never a truncated
        # comparison: reporting three coordinates against two hides the
        # difference instead of surfacing it.
        def matches?(expected, actual)
          case expected
          when Numeric
            numeric_match?(expected, actual)
          when Array
            return false unless actual.is_a?(Array) && actual.length == expected.length

            expected.zip(actual).all? { |left, right| matches?(left, right) }
          when Hash
            return false unless actual.is_a?(Hash)

            expected.all? { |key, value| matches?(value, actual[key.to_s]) }
          else
            expected == actual
          end
        end

        # Record one expected/actual pair and raise when it disagrees.
        #
        # The entry is appended before the comparison, so the failing check is
        # part of the evidence trail even though it never reaches the caller
        # inside a verified block.
        def check!(tool, checks, name, expected, actual)
          entry = { 'check' => name, 'expected' => expected, 'actual' => actual }
          checks << entry
          return entry if matches?(expected, actual)

          raise failure(tool, name, expected, actual)
        end

        def failure(tool, name, expected, actual, host = host_version)
          message = format(
            '%s did not take effect: the post-write read-back disagreed on %s ' \
            '(expected %s, read back %s); host SketchUp %s',
            tool, name, describe(expected), describe(actual), host
          )
          VerificationFailure.new(
            'schema_version' => SCHEMA_VERSION,
            'code' => ERROR_CODE,
            'tool' => tool,
            'check' => name,
            'expected' => expected,
            'actual' => actual,
            'host_version' => host,
            'message' => message
          )
        end

        # The evidence block every mutating command merges into its result.
        def verified(tool, checks, host = host_version)
          {
            'verification' => {
              'schema_version' => SCHEMA_VERSION,
              'verified' => true,
              'tool' => tool,
              'host_version' => host,
              'checks' => checks,
              'check_count' => checks.length
            }
          }
        end

        def describe(value)
          case value
          when Array then "[#{value.map { |item| describe(item) }.join(', ')}]"
          when Hash then "{#{value.map { |key, item| "#{key}: #{describe(item)}" }.join(', ')}}"
          when Float then value.inspect
          else value.to_s
          end
        end

        # --- model queries -------------------------------------------------

        def resolve(model, persistent_id)
          found = model.find_entity_by_persistent_id(persistent_id)
          found = found.first if found.is_a?(Array)
          found
        end

        def present?(entity)
          !entity.nil? && entity.valid?
        end

        # SketchUp::BoundingBox reports width = x extent, height = y extent,
        # depth = z extent.
        def extents(bounds)
          [bounds.width.to_f, bounds.height.to_f, bounds.depth.to_f]
        end

        def centre(bounds)
          point = bounds.center
          [point.x.to_f, point.y.to_f, point.z.to_f]
        end

        # --- read-back checks ----------------------------------------------

        def check_entity_present!(tool, checks, model, persistent_id)
          entity = resolve(model, persistent_id)
          check!(tool, checks, 'entity_present', persistent_id, present?(entity) ? persistent_id : nil)
          entity
        end

        def check_entity_name!(tool, checks, entity, expected_name)
          actual = entity.respond_to?(:name) ? entity.name.to_s : nil
          check!(tool, checks, 'entity_name', expected_name, actual)
        end

        def check_extents!(tool, checks, entity, expected_extents)
          return unless entity.respond_to?(:bounds)

          bounds = entity.bounds
          check!(tool, checks, 'bounds_not_empty', false, bounds.empty?)
          check!(tool, checks, 'bounds_extents', expected_extents, extents(bounds))
        end

        def check_absent!(tool, checks, model, persistent_ids, label)
          remaining = persistent_ids.select do |persistent_id|
            present?(resolve(model, persistent_id))
          end
          check!(tool, checks, "#{label}_absent", [], remaining)
        end

        # Compared in the caller's requested order, not the selection's. With
        # replace=false a pre-existing selection can interleave the new ids, and
        # an order-sensitive comparison would then report a mismatch for a
        # selection that in fact contains everything that was asked for.
        def check_selection!(tool, checks, model, persistent_ids)
          selected = model.selection.map do |item|
            item.respond_to?(:persistent_id) ? item.persistent_id : nil
          end
          landed = persistent_ids.select { |persistent_id| selected.include?(persistent_id) }
          check!(tool, checks, 'selection_contains', persistent_ids, landed)
        end

        def check_center_moved!(tool, checks, entity, before, translation)
          return unless entity.respond_to?(:bounds)

          # Rotation and scaling are applied about the entity centre, which
          # leaves the centre invariant, so translation is the only component
          # the centre must reflect after the transform.
          expected = [
            before[0] + translation[0].to_f,
            before[1] + translation[1].to_f,
            before[2] + translation[2].to_f
          ]
          check!(tool, checks, 'bounds_center', expected, centre(entity.bounds))
        end

        # An invariant, not an equality: a legitimate import can add nothing to
        # the root context, but it must never remove geometry. Recorded directly
        # rather than through check!, because check! would raise on any change
        # at all, including the increase that is the expected outcome.
        def check_count_not_decreased!(tool, checks, before, after)
          checks << {
            'check' => 'root_entity_count_not_decreased',
            'expected' => before,
            'actual' => after
          }
          return if after >= before

          raise failure(tool, 'root_entity_count_not_decreased', before, after)
        end

        def check_file_written!(tool, checks, path)
          check!(tool, checks, 'file_exists', true, File.file?(path))
          size = File.file?(path) ? File.size(path) : 0
          check!(tool, checks, 'file_non_empty', true, size.positive?)
        end

        def check_material!(tool, checks, model, expected_name, expected_values)
          material = model.materials.find { |item| item.name.to_s == expected_name }
          check!(tool, checks, 'material_present', expected_name, material ? expected_name : nil)
          return if material.nil?

          expected_values.each do |key, expected|
            actual = read_material_value(material, key)
            check!(tool, checks, "material_#{key}", expected, actual)
          end
        end

        # Texture presence only. The recorded filename is deliberately not
        # compared: SketchUp normalises texture paths per platform, so an exact
        # string match would fail on Windows separators or a case difference
        # while the texture was in fact applied correctly.
        def check_material_texture!(tool, checks, model, expected_name, expected_present = true)
          material = model.materials.find { |item| item.name.to_s == expected_name }
          actual = !material.nil? && material.respond_to?(:texture) && !material.texture.nil?
          check!(tool, checks, 'material_texture', expected_present, actual)
        end

        def check_material_absent!(tool, checks, model, expected_name)
          found = model.materials.any? { |item| item.name.to_s == expected_name }
          check!(tool, checks, 'material_absent', false, found)
        end

        def check_scene!(tool, checks, model, expected_name, expected_description = nil)
          page = model.pages.find { |item| item.name.to_s == expected_name }
          check!(tool, checks, 'scene_present', expected_name, page ? expected_name : nil)
          return if page.nil? || expected_description.nil?

          check!(tool, checks, 'scene_description', expected_description, page.description.to_s)
        end

        def check_scene_absent!(tool, checks, model, expected_name)
          found = model.pages.any? { |item| item.name.to_s == expected_name }
          check!(tool, checks, 'scene_absent', false, found)
        end

        def check_tag!(tool, checks, model, expected_name, expected_visible = nil)
          tag = model.layers.find { |item| item.name.to_s == expected_name }
          check!(tool, checks, 'tag_present', expected_name, tag ? expected_name : nil)
          return if tag.nil? || expected_visible.nil?

          check!(tool, checks, 'tag_visible', expected_visible, tag.visible?)
        end

        def check_tag_absent!(tool, checks, model, expected_name)
          found = model.layers.any? { |item| item.name.to_s == expected_name }
          check!(tool, checks, 'tag_absent', false, found)
        end

        def check_assigned_material!(tool, checks, entity, material, side)
          if %w[front both].include?(side)
            check!(tool, checks, 'entity_material', material.name.to_s, front_material_name(entity))
          end
          return unless %w[back both].include?(side)

          check!(tool, checks, 'entity_back_material', material.name.to_s, back_material_name(entity))
        end

        def check_assigned_tag!(tool, checks, entity, tag)
          actual = entity.respond_to?(:layer) && entity.layer ? entity.layer.name.to_s : nil
          check!(tool, checks, 'entity_tag', tag.name.to_s, actual)
        end

        def check_group_members!(tool, checks, group, expected_count)
          count = group.respond_to?(:entities) && group.entities.respond_to?(:length) ? group.entities.length : nil
          check!(tool, checks, 'group_member_count', expected_count, count)
        end

        private

        def numeric_match?(expected, actual)
          return false unless actual.is_a?(Numeric)

          left = expected.to_f
          right = actual.to_f
          return true if left == right

          delta = (left - right).abs
          delta <= [left.abs, right.abs].max * REL_TOLERANCE || delta <= ABS_TOLERANCE
        end

        def read_material_value(material, key)
          case key
          when 'color'
            return nil unless material.respond_to?(:color) && material.color

            color = material.color
            [color.red, color.green, color.blue]
          when 'alpha'
            material.respond_to?(:alpha) ? material.alpha.to_f : nil
          when 'texture_path'
            material.respond_to?(:texture) && material.texture ? material.texture.filename.to_s : nil
          else
            nil
          end
        end

        def front_material_name(entity)
          entity.respond_to?(:material) && entity.material ? entity.material.display_name.to_s : nil
        end

        def back_material_name(entity)
          return nil unless entity.respond_to?(:back_material) && entity.back_material

          entity.back_material.display_name.to_s
        end
      end
    end
  end
end
