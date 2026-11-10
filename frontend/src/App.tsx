import { useCallback, useEffect, useRef, useState } from 'react'
import { SearchError, searchPlaces, type ErrorCode, type SearchInput, type SearchResult } from './api'
import { formatRadius, formatRelease } from './format'
import MapView, { type AreaRequest } from './MapView'
import PlaceList from './PlaceList'
import SearchForm from './SearchForm'

const DEFAULT_WHAT = 'Coffee'
const DEFAULT_WHERE = 'Greenwich Village'

const ERROR_TITLES: Record<ErrorCode, string> = {
  invalid: 'Check your search',
  not_found: 'Location not found',
  out_of_area: 'Outside New York City',
  rate_limited: 'Daily limit reached',
  upstream_busy: 'Address lookup unavailable',
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
  // Whether the location field was edited since the last search: a chip then uses what was typed.
  const [whereEdited, setWhereEdited] = useState(false)
  const latest = useRef(0)
  const started = useRef(false)

  const run = useCallback(async (input: SearchInput) => {
    const id = ++latest.current
    setLoading(true)
    setError(null)
    setLastInput(input)
    setWhereEdited(false)
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

  // A chip searches the same place as the last search (the typed location or the map area),
  // unless a new location has been typed since.
  function chip(label: string) {
    setWhat(label)
    if (lastInput && 'lat' in lastInput && !whereEdited) run({ ...lastInput, what: label })
    else run({ what: label, where: where.trim() || DEFAULT_WHERE })
  }

  function editWhere(value: string) {
    setWhere(value)
    setWhereEdited(true)
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
          <p className="tagline">Search places across New York City&apos;s five boroughs.</p>
          <SearchForm
            what={what}
            where={where}
            busy={loading}
            activeTerm={activeTerm}
            onWhat={setWhat}
            onWhere={editWhere}
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
                    Within {formatRadius(result.query.radius_m)}
                    {result.data_release && ` · Overture Maps data, ${formatRelease(result.data_release)} release`}
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
              Place data from the <a href="https://overturemaps.org">Overture Maps Foundation</a>, built from
              sources including Meta, Microsoft, Foursquare and AllThePlaces, under the Community Data License
              Agreement (CDLA) Permissive 2.0, Apache 2.0 and Creative Commons Zero (CC0) licenses. Foursquare
              data &copy; 2024 Foursquare Labs, Inc.{' '}
              <a href="https://docs.overturemaps.org/attribution/">Full attribution</a>.
            </p>
            <p>
              Neighborhoods and borough boundaries from{' '}
              <a href="https://opendata.cityofnewyork.us/">NYC Open Data</a>; addresses located by{' '}
              <a href="https://geosearch.planninglabs.nyc/">NYC GeoSearch</a>, both from the NYC Department of City
              Planning. Map tiles &copy;{' '}
              <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>.
            </p>
            <p>
              <a href="https://github.com/rfranci52/react-google-maps">Rebuilt from a 2019 bootcamp project</a>{' '}
              that called Yelp straight from the browser.
            </p>
            <details>
              <summary>How the data and the search work</summary>
              <ul>
                <li>The places are this site&apos;s own copy of Overture Maps data for the five boroughs, refreshed monthly.</li>
                <li>Neighborhood names are looked up locally; only street addresses go to NYC GeoSearch, and each answer is kept for 30 days.</li>
                <li>Each visitor gets a daily allowance of searches and of new address lookups.</li>
              </ul>
            </details>
            <p className="privacy">
              A street address you type is sent to NYC GeoSearch. Your internet address is never stored; a salted
              hash of it counts your searches each day.
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
