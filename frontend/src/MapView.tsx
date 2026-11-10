import { useCallback, useEffect, useRef, useState } from 'react'
import { Circle, CircleMarker, MapContainer, TileLayer, Tooltip } from 'react-leaflet'
import L from 'leaflet'
import type { Place } from './api'

// Base map: OpenStreetMap's standard raster tiles, which need no key. Their usage policy
// (https://operations.osmfoundation.org/policies/tiles/, read 2026-11-09) allows interactive
// viewing like this with visible attribution, a browser Referer and normal browser caching,
// and forbids prefetching or bulk downloads, which this page never does. CARTO's dark basemap
// was the first choice, but CARTO now requires an API key for its raster tiles
// (https://github.com/CartoDB/basemap-styles, read 2026-11-09). The dark look is a CSS filter on
// the tile layer only (index.css), so the attribution stays as published. VITE_TILE_URL swaps
// the source without code changes, as the policy recommends.
const TILE_URL = import.meta.env.VITE_TILE_URL || 'https://tile.openstreetmap.org/{z}/{x}/{y}.png'
const ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'

const RADII = [500, 1000, 1500, 2500] as const
const START: [number, number] = [40.7308, -73.9973]
const ACCENT = '#5eead4'

export interface AreaRequest {
  lat: number
  lng: number
  radius: number
}

interface Props {
  places: Place[]
  center: { lat: number; lng: number } | null
  radius: number
  // Whether a new result re-frames the map. A "search this area" result keeps the visitor's view.
  fit: boolean
  busy: boolean
  selectedId: string | null
  hoveredId: string | null
  onSelect: (id: string) => void
  onHover: (id: string | null) => void
  onSearchArea: (area: AreaRequest) => void
}

// The smallest offered radius that covers the circle inside the visible map, capped at the largest.
function radiusForView(map: L.Map): number {
  const center = map.getCenter()
  const bounds = map.getBounds()
  const half = Math.min(
    center.distanceTo([bounds.getNorth(), center.lng]),
    center.distanceTo([center.lat, bounds.getEast()]),
  )
  return RADII.find((r) => r >= half) ?? RADII[RADII.length - 1]
}

export default function MapView(props: Props) {
  const { places, center, radius, fit, busy, selectedId, hoveredId, onSelect, onHover, onSearchArea } = props
  const [map, setMap] = useState<L.Map | null>(null)
  const [moved, setMoved] = useState(false)
  const markers = useRef(new Map<string, L.CircleMarker>())
  const programmatic = useRef(false)
  const baseline = useRef<{ center: L.LatLng; zoom: number } | null>(null)

  // Moves the map without it counting as the visitor exploring.
  const moveQuietly = useCallback((move: (m: L.Map) => void) => {
    if (!map) return
    programmatic.current = true
    move(map)
    programmatic.current = false
    baseline.current = { center: map.getCenter(), zoom: map.getZoom() }
  }, [map])

  // Label the map for screen readers. Leaflet already makes it focusable for keyboard panning.
  useEffect(() => {
    if (!map) return
    const el = map.getContainer()
    el.setAttribute('role', 'region')
    el.setAttribute('aria-roledescription', 'map')
    el.setAttribute('aria-label', 'Map of the results. Arrow keys pan, plus and minus zoom.')
  }, [map])

  // Leaflet only notices window resizes. The layout can also change the container's size
  // (the phone and desktop layouts differ), so it is watched directly.
  useEffect(() => {
    if (!map) return
    const observer = new ResizeObserver(() => map.invalidateSize())
    observer.observe(map.getContainer())
    return () => observer.disconnect()
  }, [map])

  // A completed search: frame the results (or the searched circle), and hide the area button.
  useEffect(() => {
    if (!map || !center) return
    setMoved(false)
    if (!fit) {
      baseline.current = { center: map.getCenter(), zoom: map.getZoom() }
      return
    }
    const circle = L.latLng(center.lat, center.lng).toBounds(radius * 2)
    const bounds = places.length
      ? L.latLngBounds(places.map((p) => [p.lat, p.lng] as [number, number])).extend([center.lat, center.lng])
      : circle
    moveQuietly((m) => m.fitBounds(bounds, { padding: [28, 28], maxZoom: 17, animate: false }))
  }, [map, places, center, radius, fit, moveQuietly])

  // Offer "Search this area" once the visitor has moved the map a meaningful amount.
  useEffect(() => {
    if (!map) return
    const onMoveEnd = () => {
      const base = baseline.current
      if (programmatic.current || !base) return
      const size = map.getSize()
      const shift = map.latLngToContainerPoint(base.center).distanceTo(L.point(size.x / 2, size.y / 2))
      if (map.getZoom() !== base.zoom || shift > Math.min(size.x, size.y) * 0.2) setMoved(true)
    }
    map.on('moveend', onMoveEnd)
    return () => {
      map.off('moveend', onMoveEnd)
    }
  }, [map])

  // A place picked in the list is brought into view if it is off the map.
  useEffect(() => {
    if (!map || !selectedId) return
    const place = places.find((p) => p.id === selectedId)
    if (place && !map.getBounds().pad(-0.1).contains([place.lat, place.lng])) {
      moveQuietly((m) => m.panTo([place.lat, place.lng], { animate: false }))
    }
  }, [map, selectedId, places, moveQuietly])

  // The highlighted marker draws above its neighbors.
  useEffect(() => {
    for (const id of [hoveredId, selectedId]) {
      if (id) markers.current.get(id)?.bringToFront()
    }
  }, [hoveredId, selectedId])

  function searchArea() {
    if (!map) return
    const c = map.getCenter().wrap()
    onSearchArea({ lat: c.lat, lng: c.lng, radius: radiusForView(map) })
  }

  return (
    <div className="map-wrap">
      <MapContainer ref={setMap} center={START} zoom={14} className="map" worldCopyJump>
        <TileLayer url={TILE_URL} attribution={ATTRIBUTION} maxZoom={19} />
        {center && (
          <Circle
            center={[center.lat, center.lng]}
            radius={radius}
            interactive={false}
            pathOptions={{ color: ACCENT, weight: 1.5, opacity: 0.7, fillOpacity: 0.05, dashArray: '4 6' }}
          />
        )}
        {places.map((p) => {
          const selected = p.id === selectedId
          const active = selected || p.id === hoveredId
          return (
            <CircleMarker
              key={p.id}
              ref={(layer) => {
                if (layer) markers.current.set(p.id, layer)
                else markers.current.delete(p.id)
              }}
              center={[p.lat, p.lng]}
              radius={active ? 10 : 6.5}
              pathOptions={{
                color: active ? ACCENT : '#0b0d10',
                weight: active ? 3 : 1.5,
                fillColor: active ? '#ffffff' : ACCENT,
                fillOpacity: 0.95,
              }}
              eventHandlers={{
                click: () => onSelect(p.id),
                mouseover: () => onHover(p.id),
                mouseout: () => onHover(null),
              }}
            >
              <Tooltip key={selected ? 'pinned' : 'hover'} direction="top" offset={[0, -10]} permanent={selected}>
                {p.name}
              </Tooltip>
            </CircleMarker>
          )
        })}
      </MapContainer>
      {moved && !busy && (
        <button type="button" className="area-button" onClick={searchArea}>
          Search this area
        </button>
      )}
    </div>
  )
}
