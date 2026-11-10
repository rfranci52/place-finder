// Small display helpers: distances, the search radius and the data release.

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

// "2026-10-21.0" reads as "October 2026".
export function formatRelease(release: string | null): string {
  const match = /^(\d{4})-(\d{2})/.exec(release ?? '')
  if (!match) return ''
  const date = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, 1))
  return date.toLocaleDateString('en-US', { month: 'long', year: 'numeric', timeZone: 'UTC' })
}

// The digits and a leading plus, for a tel: link.
export function telHref(phone: string): string {
  const digits = phone.replace(/[^\d+]/g, '')
  return `tel:${digits.startsWith('+') ? '+' + digits.slice(1).replace(/\+/g, '') : digits.replace(/\+/g, '')}`
}
