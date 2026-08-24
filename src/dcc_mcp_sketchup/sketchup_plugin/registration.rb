# frozen_string_literal: true

require 'sketchup.rb'
require 'extensions.rb'

module DccMcp
  module SketchupAdapter
    unless file_loaded?(__FILE__)
      extension = SketchupExtension.new('DCC-MCP', 'dcc_mcp_sketchup/main')
      extension.description = 'Production SketchUp adapter for the DCC-MCP ecosystem.'
      extension.version = '0.2.0' # x-release-please-version
      extension.creator = 'loonghao'
      extension.copyright = '2026 loonghao'
      Sketchup.register_extension(extension, true)
      file_loaded(__FILE__)
    end
  end
end
