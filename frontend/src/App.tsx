import { useCallback, useEffect, useRef, useState } from 'react'
import { SearchError, searchPlaces, type ErrorCode, type SearchInput, type SearchResult } from './api'
import { formatAge, formatRadius } from './format'
import MapView, { type AreaRequest } from './MapView'
import PlaceList from './PlaceList'
import SearchForm from './SearchForm'

const DEFAULT_WHAT = 'Coffee'
const DEFAULT_WHERE = 'Washington Square Park, New York'

const ERROR_TITLES: Record<ErrorCode, string> = {
  invalid: 'Check your search',
  not_found: 'Location not found',
  rate_limited: 'Daily limit reached',
  upstream_busy: 'OpenStreetMap is busy',
  unavailable: 'Search is paused',
  network: 'Connection problem',
  forbidden: 'Search unavailable',
  method_not_allowed: 'Search unavailable',
}
const RETRYABLE = new Set<ErrorCode>(['upstream_busy', 'unavailable', 'network'])

function summarize(result: SearchResult): string {
  const { term, radius_m } = result.query
  const place = result.location ?? 'the map center'
  const n = result.places.length
  if (n === 0) return `No matches for ${term.label} within ${formatRadius(radius_m)} of ${place}.`
  const count = `${n} ${n === 1 ? 'place' : 'places'}`
  return term.kind === 'category'
    ? `${count} for ${term.label} near ${place}`
    : `${count} matching “${term.label}” near ${place}`
}

export default function App() {
  const [what, setWhat] = useState(DEFAULT_WHAT)
  const [where, setWhere] = useState(DEFAULT_WHERE)
  const [result, setResult] = useState<SearchResult | null>(null)
  const [error, setError] = useState<SearchError | null>(null)
  const [loading, setLoading] = useState(false)
  const [lastInput, setLastInput] = useState<SearchInput | null>(null)
  const [fit, setFit] = useState(true)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [hoveredId, setHoveredId] = useState<string | null>(null)
  const latest = useRef(0)
  const started = useRef(false)

  const run = useCallback(async (input: SearchInput) => {
    const id = ++latest.current
    setLoading(true)
    setError(null)
    setLastInput(input)
    try {
      const found = await searchPlaces(input)
      if (id !== latest.current) return
      setResult(found)
      setFit(!('lat' in input))
      setSelectedId(null)
      setHoveredId(null)
    } catch (err) {
      if (id !== latest.current) return
      setResult(null)
      setError(err instanceof SearchError ? err : new SearchError('network', 'Something went wrong. Please try again.'))
    } finally {
      if (id === latest.current) setLoading(false)
    }
  }, [])

  // One search on arrival, so the map is never empty. The ref keeps React's development-mode
  // double effect from sending it twice.
  useEffect(() => {
    if (started.current) return
    started.current = true
    void run({ what: DEFAULT_WHAT, where: DEFAULT_WHERE })
  }, [run])

  function submit() {
    run({ what: what.trim(), where: where.trim() })
  }

  // A chip searches the same place as the last search: the typed location or the map area.
  function chip(label: string) {
    setWhat(label)
    if (lastInput && 'lat' in lastInput) run({ ...lastInput, what: label })
    else run({ what: label, where: where.trim() || DEFAULT_WHERE })
  }

  function searchArea(area: AreaRequest) {
    const term = what.trim() || result?.query.what || DEFAULT_WHAT
    run({ what: term, lat: area.lat, lng: area.lng, radius: area.radius })
  }

  const activeTerm = result?.query.term.kind === 'category' ? result.query.term.key : null
  const places = result?.places ?? []
  const status = loading
    ? 'Searching…'
    : error
      ? `${ERROR_TITLES[error.code]}. ${error.message}`
      : result
        ? summarize(result)
        : ''

  return (
    <div className="app">
      <header className="topbar">
        <a className="back" href="https://www.rakimfrancis.com">
          <span aria-hidden="true">← </span>rakimfrancis.com
        </a>
        <a className="source" href="https://github.com/rfranci52/place-finder">
          Source on GitHub
        </a>
      </header>

      <main className="layout">
        <section className="intro" aria-labelledby="title">
          <h1 id="title">Place Finder</h1>
          <p className="tagline">
            Nearby places from OpenStreetMap, the free map of the world built by volunteers.
          </p>
          <SearchForm
            what={what}
            where={where}
            busy={loading}
            activeTerm={activeTerm}
            onWhat={setWhat}
            onWhere={setWhere}
            onSubmit={submit}
            onChip={chip}
          />
        </section>

        <div className="scroll-area">
          <section className="results" aria-labelledby="results-title" aria-busy={loading}>
            <h2 id="results-title" className="sr-only">Results</h2>
            <p className="sr-only" role="status" aria-live="polite">{status}</p>
            {loading && <div className="progress" aria-hidden="true" />}

            {error && (
              <div className={`notice notice-${error.code}`} role="alert">
                <p className="notice-title">{ERROR_TITLES[error.code]}</p>
                <p>{error.message}</p>
                {RETRYABLE.has(error.code) && lastInput && (
                  <button type="button" className="secondary" onClick={() => run(lastInput)}>
                    Try again
                  </button>
                )}
              </div>
            )}

            {result && (
              <div className={loading ? 'result-block is-stale' : 'result-block'}>
                <div className="summary">
                  <p className="summary-main" aria-hidden="true">{summarize(result)}</p>
                  <p className="summary-meta">
                    {result.cached ? `Cached ${formatAge(result.fetched_at)}` : 'Fresh from OpenStreetMap'}
                    {' · within '}
                    {formatRadius(result.query.radius_m)}
                  </p>
                </div>
                {places.length === 0 ? (
                  <p className="empty">
                    Try another term, or move the map and choose <strong>Search this area</strong>.
                  </p>
                ) : (
                  <PlaceList
                    places={places}
                    selectedId={selectedId}
                    hoveredId={hoveredId}
                    onSelect={setSelectedId}
                    onHover={setHoveredId}
                  />
                )}
                {result.truncated && places.length > 0 && (
                  <p className="truncated">
                    Showing the closest {places.length}. Zoom in and use Search this area to see more.
                  </p>
                )}
              </div>
            )}
          </section>

          <footer className="credits">
            <p>
              Place data &copy;{' '}
              <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>, available under
              the Open Database License. Spot a mistake?{' '}
              <a href="https://www.openstreetmap.org/fixthemap">Report a map issue</a>.
            </p>
            <p>
              <a href="https://github.com/rfranci52/react-google-maps">Rebuilt from a 2019 bootcamp project</a>{' '}
              that called Google Maps and Yelp straight from the browser.
            </p>
            <details>
              <summary>How this demo stays inside the free services&apos; limits</summary>
              <ul>
                <li>Locations are cached for 30 days and place lists for 24 hours, so a repeat search calls nothing.</li>
                <li>Location lookups start at least one second apart, across every server instance.</li>
                <li>Each visitor gets a small daily allowance of new searches, and the whole site a fixed daily share of calls.</li>
              </ul>
            </details>
            <p className="privacy">
              What you search is sent to OpenStreetMap&apos;s Nominatim and Overpass services. Your internet
              address is never stored; a salted hash of it counts your new searches each day.
            </p>
          </footer>
        </div>

        <MapView
          places={places}
          center={result?.center ?? null}
          radius={result?.query.radius_m ?? 1500}
          fit={fit}
          busy={loading}
          selectedId={selectedId}
          hoveredId={hoveredId}
          onSelect={setSelectedId}
          onHover={setHoveredId}
          onSearchArea={searchArea}
        />
      </main>
    </div>
  )
}
