# frozen_string_literal: true

require 'json'
require 'minitest/autorun'
require 'stringio'
require 'tmpdir'
require 'timeout'

module Sketchup
  class AppObserver
  end
end unless defined?(Sketchup::AppObserver)

require_relative '../../src/dcc_mcp_sketchup/sketchup_plugin/runtime'

class RuntimeTest < Minitest::Test
  def setup
    @runtime = DccMcp::SketchupAdapter::PluginRuntime.allocate
    @runtime.instance_variable_set(:@token, 'local-secret')
  end

  def test_envelope_requires_bounded_correlated_identifiers
    envelope = {
      'jsonrpc' => '2.0',
      'id' => 'a' * 32,
      'token' => 'local-secret',
      'method' => 'model.inspect',
      'params' => {}
    }

    assert_nil @runtime.send(:validate_envelope, envelope)
    error = assert_raises(ArgumentError) do
      @runtime.send(:validate_envelope, envelope.merge('id' => 'not-a-request-id'))
    end
    assert_match(/32-character lowercase hex/, error.message)
    error = assert_raises(ArgumentError) do
      @runtime.send(:validate_envelope, envelope.merge('method' => 'ruby.eval!'))
    end
    assert_match(/dotted lowercase identifier/, error.message)
  end

  def test_oversized_response_uses_a_small_non_recursive_error
    payload = @runtime.send(
      :response_payload,
      'b' * 32,
      'result' => 'x' * (DccMcp::SketchupAdapter::MAX_MESSAGE_BYTES + 1)
    )

    response = JSON.parse(payload)
    assert_equal 'b' * 32, response['id']
    assert_equal 'response_too_large', response.dig('error', 'code')
    assert_operator payload.bytesize, :<, 1024
  end

  def test_socket_response_is_written_as_one_correlated_line
    state = DccMcp::SketchupAdapter::ConnectionState.new(request_id: 'f' * 32)

    @runtime.send(:queue_response, state, 'result' => { 'status' => 'ok' })

    response = JSON.parse(state.output)
    assert_equal 'f' * 32, response['id']
    assert_equal 'ok', response.dig('result', 'status')
    assert state.output.end_with?("\n")
    assert_equal :writing, state.phase
    assert_operator state.io_deadline, :>, Process.clock_gettime(Process::CLOCK_MONOTONIC)
  end

  def test_connection_linger_is_disabled_before_handler_ownership
    options = []
    connection = Object.new
    connection.define_singleton_method(:setsockopt) { |option| options << option }

    @runtime.send(:disable_connection_linger, connection)

    assert_equal 1, options.length
    assert_equal [false, 0], options.first.linger
  end

  def test_invalid_response_id_is_not_reflected
    closed = false
    connection = Object.new
    connection.define_singleton_method(:closed?) { closed }
    connection.define_singleton_method(:close) { closed = true }
    state = DccMcp::SketchupAdapter::ConnectionState.new(
      socket: connection,
      request_id: 'attacker-controlled'
    )
    @runtime.instance_variable_set(:@connections, { connection => state })

    @runtime.send(:queue_error_response, state, 'invalid_request', 'bad id')

    assert closed
    assert_empty @runtime.send(:connections)
  end

  def test_slow_connection_does_not_block_authenticated_request
    Dir.mktmpdir do |directory|
      slow = nil
      valid = nil
      begin
        port = start_network_runtime(directory)
        slow = TCPSocket.new('127.0.0.1', port)
        valid = TCPSocket.new('127.0.0.1', port)
        request_id = 'd' * 32
        valid.write(
          JSON.generate(
            'jsonrpc' => '2.0',
            'id' => request_id,
            'token' => 'local-secret',
            'method' => 'bridge.health',
            'params' => {}
          ) + "\n"
        )

        response = pump_responses(valid).first

        assert_equal request_id, response['id']
        assert_equal 'ok', response.dig('result', 'status')
        assert_operator(
          @runtime.send(:connections).length,
          :<=,
          DccMcp::SketchupAdapter::MAX_CONNECTIONS
        )
      ensure
        slow&.close
        valid&.close
        stop_network_runtime
      end
    end
  end

  def test_partial_utf8_request_round_trips_through_one_frame
    Dir.mktmpdir do |directory|
      client = nil
      begin
        port = start_network_runtime(directory)
        client = TCPSocket.new('127.0.0.1', port)
        request_id = '1' * 32
        payload = JSON.generate(
          'jsonrpc' => '2.0',
          'id' => request_id,
          'token' => 'local-secret',
          'method' => 'model.inspect',
          'params' => { 'label' => '中文材质' }
        ).encode(Encoding::UTF_8) + "\n"
        split = payload.index('中'.encode(Encoding::UTF_8)) + 1
        client.write(payload.byteslice(0, split))
        3.times { @runtime.stub(:check_child_liveness, nil) { @runtime.send(:poll) } }
        assert_empty @runtime.instance_variable_get(:@requests)
        client.write(payload.byteslice(split, payload.bytesize - split))

        response = pump_responses(client).first

        assert_equal request_id, response['id']
        assert_equal 'ok', response.dig('result', 'status')
      ensure
        client&.close
        stop_network_runtime
      end
    end
  end

  def test_concurrent_authenticated_connections_are_bounded_by_main_thread_queue
    Dir.mktmpdir do |directory|
      clients = []
      begin
        port = start_network_runtime(directory)
        ids = %w[4 5].map { |digit| digit * 32 }
        clients = ids.map { TCPSocket.new('127.0.0.1', port) }
        clients.zip(ids).each do |client, request_id|
          client.write(
            JSON.generate(
              'jsonrpc' => '2.0',
              'id' => request_id,
              'token' => 'local-secret',
              'method' => 'bridge.health',
              'params' => {}
            ) + "\n"
          )
        end

        responses = pump_responses(*clients)

        assert_equal ids.sort, responses.map { |response| response['id'] }.sort
        assert_equal ['busy'], responses.filter_map { |response| response.dig('error', 'code') }
        assert_equal ['ok'], responses.filter_map { |response| response.dig('result', 'status') }
      ensure
        clients.each(&:close)
        stop_network_runtime
      end
    end
  end

  def test_invalid_utf8_and_trailing_frames_are_rejected
    invalid_utf8 = DccMcp::SketchupAdapter::ConnectionState.new(
      buffer: "{\"jsonrpc\":\"2.0\",\"id\":\"#{'2' * 32}\",\"token\":\"local-secret\",\"method\":\"model.inspect\",\"params\":{\"label\":\"\xFF\"}}".b
    )
    assert_raises(ArgumentError) { @runtime.send(:process_request_payload, invalid_utf8) }

    state = DccMcp::SketchupAdapter::ConnectionState.new(buffer: +''.b, phase: :reading)
    closed = false
    socket = Object.new
    socket.define_singleton_method(:recv_nonblock) do |_size, _flags, _buffer, exception:|
      assert_equal false, exception
      "{}\n{}\n".b
    end
    socket.define_singleton_method(:closed?) { closed }
    socket.define_singleton_method(:close) { closed = true }
    state.socket = socket
    @runtime.instance_variable_set(:@connections, { socket => state })
    @runtime.send(:process_readable, state)
    assert closed
    assert_empty @runtime.send(:connections)
  end

  def test_partial_response_write_and_write_deadline
    chunks = []
    exception_modes = []
    shutdown_modes = []
    connection = Object.new
    connection.define_singleton_method(:write_nonblock) do |data, exception:|
      exception_modes << exception
      chunks << data
      [2, data.bytesize].min
    end
    connection.define_singleton_method(:shutdown) { |mode| shutdown_modes << mode }
    state = DccMcp::SketchupAdapter::ConnectionState.new(
      socket: connection,
      request_id: '3' * 32,
      phase: :writing,
      output: 'abcdef',
      output_offset: 0,
      io_deadline: Process.clock_gettime(Process::CLOCK_MONOTONIC) + 1
    )

    3.times { @runtime.send(:process_writable, state) }

    assert_equal ['abcdef', 'cdef', 'ef'], chunks
    assert_equal [false, false, false], exception_modes
    assert_equal :closing, state.phase
    assert_equal [Socket::SHUT_WR], shutdown_modes

    closed = false
    stalled = Object.new
    stalled.define_singleton_method(:closed?) { closed }
    stalled.define_singleton_method(:close) { closed = true }
    expired = DccMcp::SketchupAdapter::ConnectionState.new(
      socket: stalled,
      phase: :writing,
      io_deadline: Process.clock_gettime(Process::CLOCK_MONOTONIC) - 1
    )
    @runtime.instance_variable_set(:@connections, { stalled => expired })
    @runtime.send(:expire_connections)
    assert closed
    assert_empty @runtime.send(:connections)
  end

  def test_connection_cap_excludes_listener_from_read_set
    listener = Object.new
    @runtime.instance_variable_set(:@listener, listener)
    connections = {}
    DccMcp::SketchupAdapter::MAX_CONNECTIONS.times do
      socket = Object.new
      connections[socket] = DccMcp::SketchupAdapter::ConnectionState.new(
        socket: socket,
        phase: :waiting,
        response_queue: Queue.new,
        request_deadline: Time.now.to_f + 1
      )
    end
    @runtime.instance_variable_set(:@connections, connections)
    read_sets = []
    IO.stub(:select, lambda { |reads, _writes, _errors, _timeout|
      read_sets << reads
      nil
    }) do
      @runtime.send(:pump_socket_events)
    end

    assert_empty read_sets.first
  end

  def test_stop_timer_pump_closes_slow_clients
    Dir.mktmpdir do |directory|
      slow = nil
      begin
        port = start_network_runtime(directory)
        slow = TCPSocket.new('127.0.0.1', port)
        Timeout.timeout(2) do
          while @runtime.send(:connections).empty?
            @runtime.stub(:check_child_liveness, nil) { @runtime.send(:poll) }
            sleep(0.01)
          end
        end

        stop_network_runtime

        assert_empty @runtime.send(:connections)
      ensure
        slow&.close
        stop_network_runtime
      end
    end
  end

  def test_poll_continues_after_child_liveness_error
    Dir.mktmpdir do |directory|
      configure_runtime(directory)
      checks = 0
      child_alive = lambda do
        checks += 1
        raise Errno::EINVAL, 'unsupported probe' if checks == 1

        true
      end

      @runtime.stub(:child_alive?, child_alive) do
        @runtime.send(:poll)
        response_queue = Queue.new
        @runtime.instance_variable_get(:@requests).push(
          DccMcp::SketchupAdapter::Request.new(
            id: 'e' * 32,
            method: 'bridge.health',
            params: {},
            response_queue: response_queue,
            deadline: Time.now.to_f + 2
          )
        )
        @runtime.send(:poll)

        assert_equal 'ok', response_queue.pop.dig('result', 'status')
      end
    end
  end

  def test_stop_child_does_not_signal_an_exited_child
    @runtime.instance_variable_set(:@child_pid, 12_345)

    @runtime.stub(:child_alive?, false) do
      Process.stub(:kill, ->(*) { flunk('an exited child must not be signalled') }) do
        @runtime.send(:stop_child)
      end
    end

    assert_nil @runtime.instance_variable_get(:@child_pid)
  end

  def test_stop_child_uses_the_platform_signal_and_reaps_the_child
    @runtime.instance_variable_set(:@child_pid, 12_345)
    alive = [true, false]
    signals = []

    @runtime.stub(:child_alive?, -> { alive.shift }) do
      @runtime.stub(:child_stop_signal, 'KILL') do
        Process.stub(:kill, ->(signal, pid) { signals << [signal, pid] }) do
          @runtime.send(:stop_child)
        end
      end
    end

    assert_equal [['KILL', 12_345]], signals
    assert_nil @runtime.instance_variable_get(:@child_pid)
  end

  def test_stop_child_is_idempotent_when_windows_reports_invalid_argument
    @runtime.instance_variable_set(:@child_pid, 12_345)

    @runtime.stub(:child_alive?, true) do
      @runtime.stub(:child_stop_signal, 'KILL') do
        Process.stub(:kill, ->(*) { raise Errno::EINVAL, 'child already exited' }) do
          @runtime.send(:stop_child)
        end
      end
    end

    assert_nil @runtime.instance_variable_get(:@child_pid)
  end

  def test_connection_failure_diagnostic_does_not_record_identifier_value
    request_id = 'c' * 32
    _, diagnostic = capture_io do
      @runtime.send(
        :record_connection_failure,
        'validate',
        request_id,
        ArgumentError.new('sensitive detail'),
        'invalid_request'
      )
    end

    assert_includes diagnostic, 'code=invalid_request stage=validate error=ArgumentError'
    assert_includes diagnostic, 'id_present=true id_bytes=32 id_valid=true'
    refute_includes diagnostic, request_id
    refute_includes diagnostic, 'sensitive detail'
  end

  private

  def configure_runtime(directory)
    commands = Object.new
    commands.define_singleton_method(:execute) { |_method, _params| { 'status' => 'ok' } }
    @runtime.instance_variable_set(:@commands, commands)
    @runtime.instance_variable_set(
      :@requests,
      SizedQueue.new(DccMcp::SketchupAdapter::REQUEST_QUEUE_SIZE)
    )
    @runtime.instance_variable_set(:@stopped, false)
    @runtime.instance_variable_set(:@child_pid, 12_345)
    @runtime.instance_variable_set(:@connections, {})
    @runtime.instance_variable_set(:@log_path, File.join(directory, 'sidecar.log'))
  end

  def start_network_runtime(directory)
    configure_runtime(directory)
    listener = TCPServer.new('127.0.0.1', 0)
    listener.listen(8)
    @runtime.instance_variable_set(:@listener, listener)
    listener.addr[1]
  end

  def pump_responses(*clients)
    Timeout.timeout(2) do
      @runtime.stub(:check_child_liveness, nil) do
        loop do
          @runtime.send(:poll)
          if clients.all? { |client| IO.select([client], nil, nil, 0) }
            return clients.map { |client| JSON.parse(client.gets) }
          end
          sleep(0.005)
        end
      end
    end
  end

  def stop_network_runtime
    return unless @runtime

    @runtime.instance_variable_set(:@stopped, true)
    @runtime.send(:connections).values.each do |state|
      @runtime.send(:close_connection_state, state)
    end
    listener = @runtime.instance_variable_get(:@listener)
    listener&.close unless listener&.closed?
    @runtime.instance_variable_set(:@listener, nil)
  end
end
