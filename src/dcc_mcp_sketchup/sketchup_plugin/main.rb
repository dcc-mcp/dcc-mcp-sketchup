# frozen_string_literal: true

require_relative 'bootstrap_errors'

module DccMcp
  module SketchupAdapter
    BOOTSTRAP_ERRORS = BootstrapErrors.new(
      File.join(
        File.dirname(__dir__),
        '.dcc-mcp',
        'logs',
        'sketchup-bootstrap-errors.jsonl'
      )
    )

    class << self
      def start
        @runtime ||= PluginRuntime.new(__dir__)
        @runtime.start
      end

      def stop
        @runtime&.stop
        @runtime = nil
      end
    end

    begin
      require 'sketchup.rb'
      require_relative 'runtime'
      start
      BOOTSTRAP_ERRORS.clear
    rescue StandardError, ScriptError => e
      capture_outcome = begin
        path = BOOTSTRAP_ERRORS.capture('startup', e)
        "bootstrap error recorded at #{path}"
      rescue BootstrapErrors::CaptureFailure => capture_error
        capture_error.message
      end
      diagnostic = "DCC-MCP SketchUp failed to start: #{e.class}: #{e.message}; #{capture_outcome}"
      warn(diagnostic)
      UI.messagebox(diagnostic) if defined?(UI)
    end
  end
end
