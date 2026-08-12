# frozen_string_literal: true

require 'sketchup.rb'
require_relative 'runtime'

module DccMcp
  module SketchupAdapter
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

    start
  rescue StandardError => e
    warn("DCC-MCP SketchUp failed to start: #{e.class}: #{e.message}")
    UI.messagebox("DCC-MCP SketchUp failed to start:\n#{e.message}")
  end
end
