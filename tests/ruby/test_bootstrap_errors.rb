# frozen_string_literal: true

require 'json'
require 'minitest/autorun'
require 'tmpdir'
require_relative '../../src/dcc_mcp_sketchup/sketchup_plugin/bootstrap_errors'

class BootstrapErrorsTest < Minitest::Test
  def test_capture_writes_a_structured_record_and_success_clears_it
    Dir.mktmpdir do |directory|
      path = File.join(directory, 'sketchup-bootstrap-errors.jsonl')
      errors = DccMcp::SketchupAdapter::BootstrapErrors.new(path)

      captured = errors.capture('startup', RuntimeError.new('sidecar launch failed'))
      record = JSON.parse(File.read(path, encoding: 'UTF-8'))

      assert_equal path, captured
      assert_equal 'startup', record['stage']
      assert_equal 'RuntimeError', record['error_class']
      assert_equal 'sidecar launch failed', record['message']
      refute_nil record['timestamp']

      errors.clear
      refute File.exist?(path)
    end
  end

  def test_capture_failure_preserves_the_original_error_in_the_diagnostic
    Dir.mktmpdir do |directory|
      blocker = File.join(directory, 'not-a-directory')
      File.write(blocker, 'file')
      errors = DccMcp::SketchupAdapter::BootstrapErrors.new(File.join(blocker, 'errors.jsonl'))

      failure = assert_raises(DccMcp::SketchupAdapter::BootstrapErrors::CaptureFailure) do
        errors.capture('startup', RuntimeError.new('original startup failure'))
      end

      assert_includes failure.message, 'RuntimeError: original startup failure'
      assert_includes failure.message, 'capture failed with'
    end
  end
end
