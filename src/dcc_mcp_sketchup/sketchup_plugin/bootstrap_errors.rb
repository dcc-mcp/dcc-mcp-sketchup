# frozen_string_literal: true

require 'fileutils'
require 'json'
require 'time'

module DccMcp
  module SketchupAdapter
    class BootstrapErrors
      class CaptureFailure < StandardError; end

      def initialize(path)
        @path = File.expand_path(path)
      end

      def capture(stage, error)
        FileUtils.mkdir_p(File.dirname(@path))
        record = {
          'timestamp' => Time.now.utc.iso8601(6),
          'stage' => stage.to_s,
          'error_class' => error.class.name,
          'message' => error.message.to_s
        }
        File.open(@path, 'a:utf-8') { |file| file.puts(JSON.generate(record)) }
        @path
      rescue StandardError => capture_error
        raise CaptureFailure,
              "could not persist #{error.class}: #{error.message}; " \
              "capture failed with #{capture_error.class}: #{capture_error.message}"
      end

      def clear
        File.delete(@path) if File.file?(@path)
      rescue StandardError => error
        raise CaptureFailure,
              "could not clear bootstrap error log: #{error.class}: #{error.message}"
      end
    end
  end
end
