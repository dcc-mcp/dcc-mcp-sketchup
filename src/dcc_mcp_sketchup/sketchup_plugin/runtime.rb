# frozen_string_literal: true

require 'json'
require 'securerandom'
require 'socket'
require 'thread'
require 'tmpdir'
require_relative 'commands'

module DccMcp
  module SketchupAdapter
    MAX_MESSAGE_BYTES = 1024 * 1024
    REQUEST_ID_PATTERN = /\A[0-9a-f]{32}\z/.freeze
    METHOD_PATTERN = /\A[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+\z/.freeze
    # Network connections are multiplexed by the bounded SketchUp UI-timer pump,
    # while exactly one main-thread request remains in flight.
    REQUEST_QUEUE_SIZE = 1
    MAX_CONNECTIONS = 16

    Request = Struct.new(:id, :method, :params, :response_queue, :deadline, keyword_init: true)
    ConnectionState = Struct.new(
      :socket,
      :buffer,
      :request_id,
      :response_queue,
      :request_deadline,
      :phase,
      :output,
      :output_offset,
      :io_deadline,
      keyword_init: true
    )

    class QuitObserver < Sketchup::AppObserver
      def initialize(runtime)
        super()
        @runtime = runtime
      end

      def onQuit
        @runtime.stop
      end
    end

    class PluginRuntime
      attr_reader :log_path

      def initialize(plugin_dir)
        @plugin_dir = File.expand_path(plugin_dir)
        @commands = Commands.new
        @requests = SizedQueue.new(REQUEST_QUEUE_SIZE)
        @token = SecureRandom.urlsafe_base64(32)
        @listener = nil
        @connections = {}
        @child_pid = nil
        @log_handle = nil
        @timer_id = nil
        @observer = nil
        @stopped = false
        @log_path = File.join(Dir.tmpdir, "dcc-mcp-sketchup-#{Process.pid}.log")
      end

      def start
        return if running?
        stop if @listener || @timer_id || @observer || @child_pid || @log_handle

        @stopped = false
        @listener = TCPServer.new('127.0.0.1', 0)
        @listener.listen(8)
        port = @listener.addr[1]
        launch_sidecar(port)
        @timer_id = UI.start_timer(0.01, true) { poll }
        raise 'SketchUp could not start the DCC-MCP UI timer' if @timer_id.nil?

        @observer = QuitObserver.new(self)
        raise 'SketchUp could not register the DCC-MCP quit observer' unless Sketchup.add_observer(@observer)

        puts("DCC-MCP SketchUp started; sidecar log: #{@log_path}")
      rescue StandardError
        stop
        raise
      end

      def running?
        return false if @stopped || @listener.nil? || @child_pid.nil?

        Process.waitpid(@child_pid, Process::WNOHANG).nil?
      rescue Errno::ECHILD
        false
      end

      def poll
        return if @stopped

        pump_response_outbox
        expire_connections
        pump_socket_events
        REQUEST_QUEUE_SIZE.times do
          request = @requests.pop(true)
          request.response_queue << execute_request(request)
        rescue ThreadError
          break
        end
        pump_response_outbox
        expire_connections
        pump_socket_events
        check_child_liveness
      rescue StandardError => e
        record_connection_failure('poll', nil, e, 'runtime_error')
      end

      def stop
        return if @stopped

        @stopped = true
        if @timer_id
          UI.stop_timer(@timer_id)
          @timer_id = nil
        end
        if @observer
          Sketchup.remove_observer(@observer)
          @observer = nil
        end
        connections.values.each { |state| close_connection_state(state) }
        @listener&.close unless @listener&.closed?
        @listener = nil
        @connections = {}
        stop_child
        @log_handle&.close
        @log_handle = nil
      end

      private

      def pump_socket_events
        reading = connections.values.select { |state| %i[reading closing].include?(state.phase) }
        writing = connections.values.select { |state| state.phase == :writing }
        listeners = @listener && connections.length < MAX_CONNECTIONS ? [@listener] : []
        readable, writable = IO.select(
          [*listeners, *reading.map(&:socket)],
          writing.map(&:socket),
          nil,
          0
        )
        return unless readable || writable

        if readable&.delete(@listener)
          accept_pending_connections
        end
        readable&.each { |socket| process_readable(connections[socket]) if connections.key?(socket) }
        writable&.each { |socket| process_writable(connections[socket]) if connections.key?(socket) }
      end

      def accept_pending_connections
        while connections.length < MAX_CONNECTIONS
          connection = @listener.accept_nonblock
          connection.set_encoding(Encoding::BINARY)
          disable_connection_linger(connection)
          connections[connection] = ConnectionState.new(
            socket: connection,
            buffer: +''.b,
            phase: :reading,
            output_offset: 0,
            io_deadline: monotonic_now + 5.0
          )
        end
      rescue IO::WaitReadable, Errno::EINTR
        nil
      end

      def process_readable(state)
        return unless state

        if state.phase == :closing
          consume_peer_close(state)
          return
        end

        chunk = state.socket.recv_nonblock(65_536, 0, nil, exception: false)
        return if chunk == :wait_readable
        raise EOFError if chunk.empty?

        newline = chunk.index("\n")
        if newline && chunk.bytesize > newline + 1
          raise ArgumentError, 'request must contain exactly one newline-delimited frame'
        end
        state.buffer << (newline ? chunk.byteslice(0, newline) : chunk)
        raise ArgumentError, 'request exceeds 1 MiB' if state.buffer.bytesize > MAX_MESSAGE_BYTES
        process_request_payload(state) if newline
      rescue IO::WaitReadable
        nil
      rescue EOFError
        close_connection_state(state)
      rescue JSON::ParserError, ArgumentError, TypeError, SecurityError => e
        record_connection_failure('validate', state.request_id, e, 'invalid_request')
        queue_error_response(state, 'invalid_request', e.message)
      rescue IOError, SystemCallError => e
        record_connection_failure('read', state.request_id, e, 'bridge_error')
        close_connection_state(state)
      rescue StandardError => e
        record_connection_failure('read', state.request_id, e, 'bridge_error')
        queue_error_response(state, 'bridge_error', e.message)
      end

      def process_request_payload(state)
        payload = state.buffer.dup.force_encoding(Encoding::UTF_8)
        raise ArgumentError, 'request must be valid UTF-8' unless payload.valid_encoding?

        envelope = JSON.parse(payload)
        state.request_id = envelope['id'] if envelope.is_a?(Hash)
        validate_envelope(envelope)
        response_queue = Queue.new
        deadline = response_deadline(envelope)
        request = Request.new(
          id: state.request_id,
          method: envelope['method'],
          params: envelope.fetch('params', {}),
          response_queue: response_queue,
          deadline: deadline
        )
        @requests.push(request, true)
        state.response_queue = response_queue
        state.request_deadline = deadline
        state.phase = :waiting
        state.buffer.clear
      rescue ThreadError => e
        record_connection_failure('queue', state.request_id, e, 'busy')
        queue_error_response(state, 'busy', 'SketchUp bridge request queue is full')
      end

      def pump_response_outbox
        connections.values.select { |state| state.phase == :waiting }.each do |state|
          response = state.response_queue.pop(true)
          queue_response(state, response)
        rescue ThreadError
          next
        end
      end

      def expire_connections
        monotonic = monotonic_now
        wall_clock = Time.now.to_f
        connections.values.each do |state|
          case state.phase
          when :reading, :writing, :closing
            close_connection_state(state) if monotonic >= state.io_deadline
          when :waiting
            if wall_clock >= state.request_deadline
              queue_error_response(state, 'bridge_error', 'SketchUp main-thread response timed out')
            end
          end
        end
      end

      def process_writable(state)
        return unless state&.phase == :writing

        remaining = state.output.byteslice(state.output_offset, state.output.bytesize - state.output_offset)
        written = state.socket.write_nonblock(remaining, exception: false)
        return if written == :wait_writable
        raise IOError, 'response socket closed during write' unless written.positive?

        state.output_offset += written
        return if state.output_offset < state.output.bytesize

        shutdown_connection_write(state.socket)
        state.phase = :closing
        state.io_deadline = monotonic_now + 1.0
      rescue IO::WaitWritable
        nil
      rescue IOError, SystemCallError => e
        record_connection_failure('write', state.request_id, e, 'bridge_error')
        close_connection_state(state)
      end

      def consume_peer_close(state)
        chunk = state.socket.recv_nonblock(65_536, 0, nil, exception: false)
        return if chunk == :wait_readable

        raise EOFError if chunk.empty?
      rescue IO::WaitReadable
        nil
      rescue EOFError, IOError, SystemCallError
        close_connection_state(state)
      end

      def validate_envelope(envelope)
        raise ArgumentError, 'request must be an object' unless envelope.is_a?(Hash)
        raise ArgumentError, "jsonrpc must be '2.0'" unless envelope['jsonrpc'] == '2.0'
        raise SecurityError, 'invalid bridge token' unless secure_compare(envelope['token'], @token)
        raise ArgumentError, 'id must be a 32-character lowercase hex string' unless valid_request_id?(envelope['id'])
        method = envelope['method']
        unless method.is_a?(String) && method.bytesize <= 128 && METHOD_PATTERN.match?(method)
          raise ArgumentError, 'method must be a dotted lowercase identifier no longer than 128 bytes'
        end
        params = envelope.fetch('params', {})
        raise ArgumentError, 'params must be an object' unless params.is_a?(Hash)
      end

      def execute_request(request)
        result = if request.method == 'bridge.health'
                   raise ArgumentError, 'bridge.health accepts no parameters' unless request.params.empty?

                   @commands.execute('diagnostics.ping', {})
                 else
                   @commands.execute(request.method, request.params.dup)
                 end
        { 'result' => result }
      rescue StandardError => e
        { 'error' => { 'code' => 'host_error', 'message' => e.message } }
      end

      def queue_response(state, response)
        state.output = "#{response_payload(state.request_id, response)}\n"
        state.output_offset = 0
        state.phase = :writing
        state.io_deadline = monotonic_now + 1.0
      end

      def queue_error_response(state, code, message)
        unless valid_request_id?(state.request_id)
          close_connection_state(state)
          return
        end

        queue_response(
          state,
          'error' => { 'code' => code, 'message' => message.to_s.empty? ? code : message.to_s }
        )
      end

      def response_payload(request_id, response)
        response_id = valid_request_id?(request_id) ? request_id : nil
        payload = JSON.generate({ 'jsonrpc' => '2.0', 'id' => response_id }.merge(response))
        return payload if payload.bytesize <= MAX_MESSAGE_BYTES

        JSON.generate(
          'jsonrpc' => '2.0',
          'id' => response_id,
          'error' => { 'code' => 'response_too_large', 'message' => 'Response exceeds 1 MiB' }
        )
      end

      def close_connection(connection)
        return if connection.closed?

        disable_connection_linger(connection)
        close_socket(connection) unless connection.closed?
      rescue StandardError
        nil
      end

      def close_socket(connection)
        connection.close
      end

      def close_connection_state(state)
        return unless state

        connections.delete(state.socket)
        close_connection(state.socket)
      end

      def disable_connection_linger(connection)
        return unless connection.respond_to?(:setsockopt)

        connection.setsockopt(Socket::Option.linger(false, 0))
      rescue IOError, SystemCallError, TypeError, ArgumentError
        nil
      end

      def shutdown_connection_write(connection)
        return unless connection.respond_to?(:shutdown)

        connection.shutdown(Socket::SHUT_WR)
      rescue IOError, SystemCallError
        nil
      end

      def record_connection_failure(stage, request_id, error, code)
        id_bytes = request_id.respond_to?(:bytesize) ? request_id.bytesize : -1
        warn(
          "DCC-MCP SketchUp bridge diagnostic: code=#{code} stage=#{stage} error=#{error.class} " \
          "id_present=#{!request_id.nil?} id_bytes=#{id_bytes} id_valid=#{valid_request_id?(request_id)}"
        )
      rescue IOError, SystemCallError
        nil
      end

      def response_deadline(envelope)
        supplied = envelope.fetch('params', {})['_dcc_mcp_deadline_unix_ms']
        return Time.now.to_f + 8.0 if envelope['method'] == 'bridge.health'
        if supplied
          deadline = Integer(supplied) / 1000.0
          now = Time.now.to_f
          unless deadline > now && deadline <= now + 3600.0
            raise ArgumentError, 'request deadline must be within the next 3600 seconds'
          end

          return deadline
        end

        Time.now.to_f + 620.0
      end

      def connections
        @connections ||= {}
      end

      def monotonic_now
        Process.clock_gettime(Process::CLOCK_MONOTONIC)
      end

      def secure_compare(left, right)
        left = left.to_s.b
        right = right.to_s.b
        return false unless left.bytesize == right.bytesize

        difference = 0
        left.bytes.zip(right.bytes) { |a, b| difference |= a ^ b }
        difference.zero?
      end

      def valid_request_id?(value)
        value.is_a?(String) && REQUEST_ID_PATTERN.match?(value)
      end

      def launch_sidecar(port)
        configured = ENV.fetch('DCC_MCP_SKETCHUP_SERVER', '').strip
        server = configured.empty? ? read_server_path : File.expand_path(configured)
        raise "DCC-MCP SketchUp server not found: #{server}" unless File.file?(server)

        env = ENV.to_h.merge(
          'DCC_MCP_SKETCHUP_BRIDGE_HOST' => '127.0.0.1',
          'DCC_MCP_SKETCHUP_BRIDGE_PORT' => port.to_s,
          'DCC_MCP_SKETCHUP_BRIDGE_TOKEN' => @token,
          'DCC_MCP_SKETCHUP_VERSION' => Sketchup.version.to_s,
          'DCC_MCP_SKETCHUP_HOST_PID' => Process.pid.to_s
        )
        if RUBY_PLATFORM.match?(/mswin|mingw/)
          env['DCC_MCP_UI_CONTROL_BACKEND'] ||= 'windows-uia'
          env['DCC_MCP_UI_CONTROL_UIA_PROCESS_ID'] = Process.pid.to_s
        end
        @log_handle = File.open(@log_path, 'a:utf-8')
        @child_pid = Process.spawn(
          env,
          server,
          'serve',
          '--host-pid', Process.pid.to_s,
          '--bridge-port', port.to_s,
          out: @log_handle,
          err: [:child, :out]
        )
      end

      def read_server_path
        path_file = File.join(@plugin_dir, 'server_path.txt')
        raise 'server_path.txt is missing; reinstall the SketchUp extension' unless File.file?(path_file)

        File.expand_path(File.read(path_file, encoding: 'UTF-8').strip)
      end

      def child_alive?
        return false unless @child_pid

        Process.waitpid(@child_pid, Process::WNOHANG).nil?
      rescue Errno::ECHILD
        false
      end

      def check_child_liveness
        return unless @child_pid
        return if child_alive?

        puts("DCC-MCP SketchUp sidecar exited; see #{@log_path}")
        @child_pid = nil
      rescue StandardError => e
        record_connection_failure('child_liveness', nil, e, 'runtime_error')
      end

      def stop_child
        return unless @child_pid
        return unless child_alive?

        Process.kill(child_stop_signal, @child_pid)
        30.times do
          return @child_pid = nil unless child_alive?

          sleep(0.1)
        end
        Process.kill('KILL', @child_pid)
        Process.waitpid(@child_pid)
      rescue Errno::ESRCH, Errno::ECHILD, Errno::EINVAL
        nil
      ensure
        @child_pid = nil
      end

      def child_stop_signal
        RUBY_PLATFORM.match?(/mswin|mingw/) ? 'KILL' : 'TERM'
      end
    end
  end
end
