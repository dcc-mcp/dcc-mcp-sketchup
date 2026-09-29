# frozen_string_literal: true

# An in-memory stand-in for the SketchUp Ruby API.
#
# These fakes exist so the Ruby contract tests can exercise the real read-back
# path instead of stubbing it out. That matters: the read-back is the thing
# under test, and a fake that cannot disagree with the caller would make every
# verification pass vacuously.
#
# So the fakes model state (a persistent-id index, mutable bounds, collections
# that really add and remove) and deliberately expose seams that let a test
# make the read-back fail -- see `drift_bounds` on FakeGroup and the
# `*_result` toggles on the collections. Those seams are how the contract tests
# prove a disagreement is reported rather than swallowed.
#
# This is a stub of the API surface the adapter uses. It is not a SketchUp
# emulator, and it is not host evidence: see compat_matrix.json.

module Sketchup
  class << self
    attr_accessor :active_model

    def version
      '2026.0'
    end
  end

  PAGE_USE_ALL = 1

  class Color
    attr_reader :red, :green, :blue

    def initialize(red, green, blue)
      @red = red
      @green = green
      @blue = blue
    end
  end

  class Material
    attr_accessor :name
    attr_accessor :color, :alpha

    def initialize(name)
      @name = name
      @color = Sketchup::Color.new(0, 0, 0)
      @alpha = 1.0
      @texture = nil
    end

    def display_name
      @name
    end

    def texture
      @texture
    end

    def texture=(value)
      @texture = value.nil? ? nil : FakeTexture.new(value.to_s)
    end
  end

  class Page
    attr_accessor :name, :description

    def initialize(name)
      @name = name
      @description = ''
    end

    def update(_flags)
      true
    end
  end

  class Layer
    attr_accessor :name

    def initialize(name)
      @name = name
      @visible = true
    end

    def visible?
      @visible
    end

    def visible=(value)
      @visible = value ? true : false
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

  # Only the composition the adapter relies on: translation is the component
  # that moves an entity's centre, and rotation/scaling about the centre leave
  # it invariant. Modelling that is what lets the transform read-back assert a
  # concrete expected centre instead of "something happened".
  class Transformation
    attr_reader :translation

    def self.translation(vector)
      new(vector)
    end

    def self.rotation(_center, _axis, _degrees)
      new([0, 0, 0])
    end

    def self.scaling(_center, *_scale)
      new([0, 0, 0])
    end

    def initialize(translation)
      @translation = translation
    end

    def *(other)
      Transformation.new(
        [
          translation[0] + other.translation[0],
          translation[1] + other.translation[1],
          translation[2] + other.translation[2]
        ]
      )
    end
  end
end

class Numeric
  def degrees
    self
  end
end

class FakeTexture
  attr_reader :filename

  def initialize(filename)
    @filename = filename
  end
end

class FakeBounds
  attr_accessor :width, :height, :depth
  attr_accessor :ox, :oy, :oz

  def initialize(width = 1, height = 1, depth = 1, ox = 0, oy = 0, oz = 0)
    @width = width
    @height = height
    @depth = depth
    @ox = ox
    @oy = oy
    @oz = oz
  end

  def empty?
    false
  end

  def min
    Geom::Point3d.new(@ox, @oy, @oz)
  end

  def max
    Geom::Point3d.new(@ox + @width, @oy + @height, @oz + @depth)
  end

  def center
    Geom::Point3d.new(@ox + (@width / 2.0), @oy + (@height / 2.0), @oz + (@depth / 2.0))
  end
end

class FakeFace
  attr_reader :pushed

  def initialize(owner, points)
    @owner = owner
    @points = points
    @pushed = nil
    @normal = Struct.new(:z).new(1)
  end

  def normal
    @normal
  end

  def reverse!
    @normal = Struct.new(:z).new(-1)
    nil
  end

  def pushpull(value)
    @pushed = value
    @owner.apply_extents(@points, value)
  end
end

class FakeGroupEntities
  attr_reader :face

  def initialize(group)
    @group = group
    @values = []
    @circle_points = []
  end

  def add_face(argument)
    points = argument.is_a?(Array) && argument.first.is_a?(Geom::Point3d) ? argument : @circle_points
    @face = FakeFace.new(@group, points)
  end

  # SketchUp's add_circle produces true arc edges, not straight segments, so the
  # face's bounding box is the full diameter no matter how many segments were
  # requested. Modelling the polygon instead would make the extents read-back
  # fail at low segment counts for a cylinder that is in fact correct.
  def add_circle(center, _axis, radius, _segments)
    @circle_points = [
      Geom::Point3d.new(center.x - radius, center.y - radius, center.z),
      Geom::Point3d.new(center.x + radius, center.y - radius, center.z),
      Geom::Point3d.new(center.x + radius, center.y + radius, center.z),
      Geom::Point3d.new(center.x - radius, center.y + radius, center.z)
    ]
    %i[edge1 edge2 edge3]
  end

  def length
    @values.length
  end

  def to_a
    @values.dup
  end

  def <<(entity)
    @values << entity
    self
  end
end

class FakeLayer
  def name
    'Untagged'
  end
end

class FakeGroup
  attr_accessor :name, :material, :back_material, :layer
  attr_reader :entities

  def initialize(members = [])
    @entities = FakeGroupEntities.new(self)
    members.each { |member| @entities << member }
    @bounds = FakeBounds.new(0, 0, 0)
    @name = ''
    @material = nil
    @back_material = nil
    @layer = FakeLayer.new
    @valid = true
    @id = nil
  end

  # Assigned lazily so a group created outside the model still answers
  # consistently once the model indexes it.
  def persistent_id
    @id ||= FakeIds.next
  end

  def entityID
    persistent_id + 1
  end

  def typename
    'Group'
  end

  def valid?
    @valid
  end

  def invalidate!
    @valid = false
  end

  def hidden?
    false
  end

  def locked?
    false
  end

  def bounds
    @bounds
  end

  # Test seam: make the geometry disagree with what was requested, so the
  # read-back has something real to catch.
  def drift_bounds(width, height, depth)
    @bounds = FakeBounds.new(width, height, depth, @bounds.ox, @bounds.oy, @bounds.oz)
  end

  def translate(vector)
    @bounds = FakeBounds.new(
      @bounds.width, @bounds.height, @bounds.depth,
      @bounds.ox + vector[0], @bounds.oy + vector[1], @bounds.oz + vector[2]
    )
  end

  def apply_extents(points, height)
    xs = points.map(&:x)
    ys = points.map(&:y)
    zs = points.map(&:z)
    @bounds = FakeBounds.new(xs.max - xs.min, ys.max - ys.min, (zs.max + height) - zs.min)
  end
end

module FakeIds
  @next = 100

  def self.next
    @next += 1
  end

  # Reset between tests so a persistent id asserted in one test is not shifted
  # by however many entities earlier tests happened to create.
  def self.reset!
    @next = 100
  end
end

# Enumerable collection with the explicit "make this call fail" seams the
# command layer's own error paths need.
class FakeCollection
  include Enumerable

  def initialize
    @values = []
  end

  def each(&block)
    @values.each(&block)
  end

  def length
    @values.length
  end

  def [](index)
    @values[index]
  end

  def add_existing(item)
    @values << item
    item
  end

  protected

  attr_reader :values
end

class FakeEntities < FakeCollection
  def initialize(model)
    super()
    @model = model
  end

  # SketckUp's Entities#add_group accepts either a variadic list or a single
  # array of entities. Flatten so both call shapes produce one member per
  # entity -- without this the member count read-back compares a nested array
  # against a flat one and reports a mismatch that is an artifact of the fake.
  def add_group(*members)
    group = FakeGroup.new(members.flatten)
    @model.register(group)
    @values << group
    group
  end

  def erase_entities(list)
    list.each do |entity|
      @values.delete(entity)
      @model.unregister(entity.persistent_id)
      entity.invalidate! if entity.respond_to?(:invalidate!)
    end
    list
  end

  def transform_entities(transformation, list)
    list.each { |entity| entity.translate(transformation.translation) }
    list
  end
end

class FakeMaterials < FakeCollection
  attr_accessor :remove_result

  def initialize
    super
    @remove_result = true
  end

  def add(name)
    material = Sketchup::Material.new(name)
    @values << material
    material
  end

  def remove(material)
    @values.delete(material) if @remove_result
    @remove_result
  end
end

class FakePages < FakeCollection
  attr_accessor :erase_result

  def initialize
    super
    @erase_result = true
  end

  def add(name)
    page = Sketchup::Page.new(name)
    @values << page
    page
  end

  def erase(page)
    @values.delete(page) if @erase_result
    @erase_result
  end
end

class FakeLayers < FakeCollection
  attr_accessor :remove_result

  def initialize
    super
    @remove_result = true
    @values << Sketchup::Layer.new('Untagged')
  end

  def add(name)
    layer = Sketchup::Layer.new(name)
    @values << layer
    layer
  end

  def remove(layer)
    @values.delete(layer) if @remove_result
    @remove_result
  end
end

class FakeSelection < FakeCollection
  def clear
    @values.clear
  end

  def add(list)
    list.each { |entity| @values << entity unless @values.include?(entity) }
    self
  end
end

class FakeModel
  attr_reader :entities, :materials, :pages, :layers, :selection, :definitions
  attr_reader :commits, :aborts, :import_call, :export_call
  attr_accessor :modified

  def modified?
    @modified
  end

  def initialize
    @entities = FakeEntities.new(self)
    @materials = FakeMaterials.new
    @pages = FakePages.new
    @layers = FakeLayers.new
    @selection = FakeSelection.new
    @definitions = []
    @commits = 0
    @aborts = 0
    @index = {}
    @path = ''
    @modified = true
  end

  def valid?
    true
  end

  def title
    'Fake Model'
  end

  def name
    'Fake Model'
  end

  def guid
    'fake-guid'
  end

  def path
    @path
  end

  def active_layer
    @layers[0]
  end

  def active_path
    nil
  end

  def bounds
    FakeBounds.new(0, 0, 0)
  end

  def register(entity)
    @index[entity.persistent_id] = entity
    entity
  end

  def unregister(persistent_id)
    @index.delete(persistent_id)
  end

  def find_entity_by_persistent_id(persistent_id)
    @index[persistent_id]
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

  def save(target = nil)
    destination = target || @path
    return false if destination.to_s.empty?

    File.write(destination, 'fake sketchup model')
    @path = destination
    @modified = false
    true
  end

  def save_copy(target)
    File.write(target, 'fake sketchup model copy')
    true
  end

  def import(path, options = nil)
    @import_call = [path, options]
    true
  end

  def export(path, options = nil)
    @export_call = [path, options]
    File.write(path, 'fake export payload')
    true
  end
end
