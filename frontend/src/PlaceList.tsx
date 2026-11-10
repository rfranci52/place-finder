import { useEffect } from 'react'
import type { Place } from './api'
import { formatDistance, telHref } from './format'

interface Props {
  places: Place[]
  selectedId: string | null
  hoveredId: string | null
  onSelect: (id: string) => void
  onHover: (id: string | null) => void
}

const domId = (id: string) => `place-${id.replace('/', '-')}`

export default function PlaceList({ places, selectedId, hoveredId, onSelect, onHover }: Props) {
  // A marker clicked on the map scrolls its entry into view, on layouts where the list scrolls
  // on its own. On a phone the whole page scrolls, and jumping away from the map would be worse.
  useEffect(() => {
    if (!selectedId || !window.matchMedia('(min-width: 768px)').matches) return
    document.getElementById(domId(selectedId))?.scrollIntoView({ block: 'nearest' })
  }, [selectedId])

  return (
    <ol className="places" aria-label="Places">
      {places.map((p) => {
        const selected = p.id === selectedId
        const classes = ['place', selected && 'is-selected', p.id === hoveredId && 'is-hovered'].filter(Boolean)
        return (
          <li
            key={p.id}
            id={domId(p.id)}
            className={classes.join(' ')}
            onMouseEnter={() => onHover(p.id)}
            onMouseLeave={() => onHover(null)}
          >
            <h3 className="place-name">
              <button
                type="button"
                aria-current={selected ? 'true' : undefined}
                onClick={() => onSelect(p.id)}
                onFocus={() => onHover(p.id)}
                onBlur={() => onHover(null)}
              >
                {p.name}
              </button>
            </h3>
            <p className="place-meta">
              {p.category} <span aria-hidden="true">·</span> {formatDistance(p.distance_m)}
              <span className="sr-only"> away</span>
            </p>
            {p.address && <p className="place-line">{p.address}</p>}
            {p.hours && (
              <p className="place-line place-hours">
                <span className="sr-only">Opening hours: </span>
                {p.hours}
              </p>
            )}
            <p className="place-links">
              {p.phone && <a href={telHref(p.phone)}>{p.phone}</a>}
              {p.website && (
                <a href={p.website} target="_blank" rel="noopener noreferrer">
                  Website<span className="sr-only"> of {p.name} (opens in a new tab)</span>
                </a>
              )}
              <a href={p.osm_url} target="_blank" rel="noopener noreferrer">
                OpenStreetMap<span className="sr-only"> entry for {p.name} (opens in a new tab)</span>
              </a>
            </p>
          </li>
        )
      })}
    </ol>
  )
}
