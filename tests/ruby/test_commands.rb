# frozen_string_literal: true

require 'minitest/autorun'
require 'tmpdir'

module Sketchup
  class << self
    attr_accessor :active_model

    def version
      '2026.0'
    end
  end

  class Color
    attr_reader :red, :green, :blue

    def initialize(red, green, blue)
      @red = red
      @green = green
      @blue = blue
    end
  end
end

module Geom
  class Point3d
    attr_reader :x, :y, :z

    def initialize(x, y, z)
      @x = x
      @y = y
      @z = z
    end
  end
end

class FakeBounds
  attr_reader :min, :max

  def initialize
    @min = Geom::Point3d.new(0, 0, 0)
    @max = Geom::Point3d.new(1, 1, 1)
  end

  def empty?
    false
  end

  def width
    1
  end

  def height
    1
  end

  def depth
    1
  end
end

class FakeFace
  attr_reader :pushed

  def normal
    Struct.new(:z).new(1)
  end

  def reverse!
    nil
  end

  def pushpull(value)
    @pushed = value
  end
end

class FakeGroupEntities
  attr_reader :face

  def add_face(_points)
    @face = FakeFace.new
  end

  def add_circle(_center, _axis, _radius, _segments)
    %i[edge1 edge2 edge3]
  end
end

class FakeLayer
  def name
    'Untagged'
  end
end

class FakeGroup
  attr_accessor :name
  attr_reader :entities

  def initialize
    @entities = FakeGroupEntities.new
  end

  def persistent_id
    101
  end

  def entityID
    7
  end

  def typename
    'Group'
  end

  def valid?
    true
  end

  def hidden?
    false
  end

  def locked?
    false
  end

  def layer
    FakeLayer.new
  end

  def material
    nil
  end

  def bounds
    FakeBounds.new
  end
end

class FakeEntities
  include Enumerable

  attr_reader :group

  def initialize
    @values = []
  end

  def each(&block)
    @values.each(&block)
  end

  def add_group(*_args)
    @group = FakeGroup.new
    @values << @group
    @group
  end

  def length
    @values.length
  end

  def to_a
    @values.dup
  end
end

class FakeMaterial
  attr_reader :name

  def initialize(name)
    @name = name
  end

  def display_name
    @name
  end
end

class FakeMaterials
  include Enumerable

  attr_accessor :remove_result

  def initialize
    @values = []
    @remove_result = true
  end

  def each(&block)
    @values.each(&block)
  end

  def add_existing(material)
    @values << material
  end

  def remove(material)
    @values.delete(material) if @remove_result
    @remove_result
  end
end

class FakeModel
  attr_reader :entities, :materials, :definitions, :commits, :aborts, :import_call, :export_call

  def initialize
    @entities = FakeEntities.new
    @materials = FakeMaterials.new
    @definitions = []
    @commits = 0
    @aborts = 0
  end

  def valid?
    true
  end

  def start_operation(_name, _disable_ui)
    true
  end

  def commit_operation
    @commits += 1
    true
  end

  def abort_operation
    @aborts += 1
    true
  end

  def import(path, options = nil)
    @import_call = [path, options]
    true
  end

  def export(path, options = nil)
    @export_call = [path, options]
    true
  end
end

require_relative '../../src/dcc_mcp_sketchup/sketchup_plugin/commands'

class CommandsTest < Minitest::Test
  def setup
    Sketchup.active_model = FakeModel.new
    @commands = DccMcp::SketchupAdapter::Commands.new
  end

  def test_ping_reports_bounded_command_map
    result = @commands.execute('diagnostics.ping', {})

    assert_equal 'ok', result['status']
    assert_equal '2026.0', result['sketchup_version']
    assert_equal 28, result['command_count']
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
    result = @commands.execute(
      'geometry.add_box',
      'width' => 2.0,
      'depth' => 1.0,
      'height' => 0.5,
      'unit' => 'meters',
      'name' => 'RubySmoke'
    )

    assert_equal 101, result.dig('entity', 'persistent_id')
    assert_equal 'RubySmoke', Sketchup.active_model.entities.group.name
    assert_in_delta 19.685, Sketchup.active_model.entities.group.entities.face.pushed, 0.001
    assert_equal 1, Sketchup.active_model.commits
    assert_equal 0, Sketchup.active_model.aborts
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

      assert_equal path, Sketchup.active_model.import_call[0]
      assert_equal({ units: 'model', merge_coplanar_faces: true }, Sketchup.active_model.import_call[1])
    end
  end

  def test_export_accepts_current_official_format_and_symbolizes_options
    Dir.mktmpdir do |directory|
      path = File.join(directory, 'scene.glb')

      @commands.execute(
        'model.export',
        'path' => path,
        'options' => { 'selectionset_only' => false }
      )

      assert_equal path, Sketchup.active_model.export_call[0]
      assert_equal({ selectionset_only: false }, Sketchup.active_model.export_call[1])
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
    material = FakeMaterial.new('Surface')
    Sketchup.active_model.materials.add_existing(material)
    Sketchup.active_model.materials.remove_result = false

    error = assert_raises(RuntimeError) do
      @commands.execute('materials.remove', 'name' => 'Surface')
    end

    assert_match(/could not remove material/, error.message)
    assert_equal 0, Sketchup.active_model.commits
    assert_equal 1, Sketchup.active_model.aborts
  end

  def test_string_parameters_do_not_coerce_other_json_types
    error = assert_raises(ArgumentError) do
      @commands.execute(
        'geometry.add_box',
        'width' => 1,
        'depth' => 1,
        'height' => 1,
        'name' => 42
      )
    end

    assert_match(/must be a string/, error.message)
  end
end
