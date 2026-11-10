// Typed client for POST /api/search (frontend/api/search.py). Every failure, including a
// network error, comes back as a SearchError with a code the page knows how to show.

export interface Place {
  id: string
  name: string
  category: string
  address: string
  phone: string
  hours: string
  website: string
  lat: number
  lng: number
  osm_url: string
  distance_m: number
}

export interface Term {
  kind: 'category' | 'name'
  key: string
  label: string
}

export interface SearchResult {
  ok: true
  query: { what: string; term: Term; radius_m: number }
  center: { lat: number; lng: number }
  location: string | null
  places: Place[]
  truncated: boolean
  cached: boolean
  fetched_at: string
}

export type SearchInput =
  | { what: string; where: string }
  | { what: string; lat: number; lng: number; radius: number }

export type ErrorCode =
  | 'invalid'
  | 'forbidden'
  | 'not_found'
  | 'method_not_allowed'
  | 'rate_limited'
  | 'upstream_busy'
  | 'unavailable'
  | 'network'

export class SearchError extends Error {
  readonly code: ErrorCode
  readonly retryAfter: number | null

  constructor(code: ErrorCode, message: string, retryAfter: number | null = null) {
    super(message)
    this.name = 'SearchError'
    this.code = code
    this.retryAfter = retryAfter
  }
}

const KNOWN_CODES = new Set<ErrorCode>([
  'invalid', 'forbidden', 'not_found', 'method_not_allowed', 'rate_limited', 'upstream_busy', 'unavailable',
])
const NETWORK_MESSAGE = "Couldn't reach the search service. Check your connection and try again."

function isResult(body: unknown): body is SearchResult {
  const b = body as SearchResult | null
  return !!b && b.ok === true && Array.isArray(b.places) && typeof b.center?.lat === 'number'
}

function toError(body: unknown, status: number): SearchError {
  const error = (body as { error?: { code?: unknown; message?: unknown; retry_after?: unknown } } | null)?.error
  const code = typeof error?.code === 'string' && KNOWN_CODES.has(error.code as ErrorCode)
    ? (error.code as ErrorCode)
    : 'network'
  const message = typeof error?.message === 'string' && error.message ? error.message : `Search failed (${status}).`
  const retryAfter = typeof error?.retry_after === 'number' ? error.retry_after : null
  return new SearchError(code, code === 'network' ? NETWORK_MESSAGE : message, retryAfter)
}

export async function searchPlaces(input: SearchInput, signal?: AbortSignal): Promise<SearchResult> {
  let response: Response
  try {
    response = await fetch('/api/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(input),
      signal,
    })
  } catch (err) {
    if ((err as Error).name === 'AbortError') throw err
    throw new SearchError('network', NETWORK_MESSAGE)
  }
  let body: unknown = null
  try {
    body = await response.json()
  } catch {
    // Not JSON, for example a proxy's error page: reported as a network problem below.
  }
  if (response.ok && isResult(body)) return body
  throw toError(body, response.status)
}
