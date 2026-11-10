// Small display helpers: distances, the search radius, and how old cached data is.

const MILE_LOCALES = /^en-(US|LR)|^my\b/i
const useMiles = MILE_LOCALES.test(typeof navigator === 'undefined' ? 'en-US' : navigator.language || 'en-US')

export function formatDistance(meters: number): string {
  if (useMiles) {
    const miles = meters / 1609.344
    if (miles < 0.1) return `${Math.max(Math.round(meters * 3.28084 / 10) * 10, 10)} ft`
    return `${miles.toFixed(miles < 10 ? 1 : 0)} mi`
  }
  if (meters < 1000) return `${Math.max(Math.round(meters / 10) * 10, 10)} m`
  return `${(meters / 1000).toFixed(1)} km`
}

export function formatRadius(meters: number): string {
  return useMiles ? `${(meters / 1609.344).toFixed(1)} mi` : `${(meters / 1000).toFixed(1)} km`
}

const relative = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' })

export function formatAge(iso: string, now = Date.now()): string {
  const seconds = Math.round((Date.parse(iso) - now) / 1000)
  if (!Number.isFinite(seconds) || seconds > -60) return 'just now'
  const minutes = Math.round(seconds / 60)
  if (minutes > -60) return relative.format(minutes, 'minute')
  return relative.format(Math.round(minutes / 60), 'hour')
}

// The digits and a leading plus, for a tel: link.
export function telHref(phone: string): string {
  const digits = phone.replace(/[^\d+]/g, '')
  return `tel:${digits.startsWith('+') ? '+' + digits.slice(1).replace(/\+/g, '') : digits.replace(/\+/g, '')}`
}
