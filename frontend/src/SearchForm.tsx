import type { FormEvent } from 'react'

const CHIPS = ['Coffee', 'Pizza', 'Pharmacy', 'Clinic', 'Grocery', 'Park'] as const

interface Props {
  what: string
  where: string
  busy: boolean
  activeTerm: string | null
  onWhat: (value: string) => void
  onWhere: (value: string) => void
  onSubmit: () => void
  onChip: (chip: string) => void
}

// Buttons use aria-disabled rather than disabled while a search runs, so the button that
// was just pressed keeps keyboard focus.
export default function SearchForm({ what, where, busy, activeTerm, onWhat, onWhere, onSubmit, onChip }: Props) {
  function submit(event: FormEvent) {
    event.preventDefault()
    if (!busy) onSubmit()
  }

  return (
    <form className="search" role="search" aria-label="Search places" onSubmit={submit}>
      <div className="fields">
        <div className="field">
          <label htmlFor="what">What</label>
          <input
            id="what"
            value={what}
            onChange={(e) => onWhat(e.target.value)}
            maxLength={40}
            required
            autoComplete="off"
            enterKeyHint="search"
            placeholder="coffee, pharmacy, a name"
          />
        </div>
        <div className="field">
          <label htmlFor="where">Where</label>
          <input
            id="where"
            value={where}
            onChange={(e) => onWhere(e.target.value)}
            maxLength={80}
            required
            enterKeyHint="search"
            placeholder="Neighborhood, address or ZIP code"
          />
        </div>
        <button type="submit" className="primary" aria-disabled={busy}>
          {busy ? 'Searching…' : 'Search'}
        </button>
      </div>
      <div className="chips" role="group" aria-label="Quick searches">
        {CHIPS.map((chip) => (
          <button
            key={chip}
            type="button"
            className="chip"
            aria-pressed={activeTerm === chip.toLowerCase()}
            aria-disabled={busy}
            onClick={() => !busy && onChip(chip)}
          >
            {chip}
          </button>
        ))}
      </div>
    </form>
  )
}
