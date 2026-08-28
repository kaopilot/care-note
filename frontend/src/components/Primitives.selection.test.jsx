/**
 * `readSelectionRange` — turning a browser selection into character offsets.
 *
 * This is the piece of the client most worth testing and the piece that was
 * most recently wrong. It is pure arithmetic over the DOM, it has no network
 * and no state, and when it is wrong it is wrong *silently*: the highlight is
 * created successfully, at the wrong offsets, pointing a provenance pointer at
 * words nobody selected. In a system whose whole argument is that every claim
 * traces back to its source, a citation that lands three characters off is a
 * worse failure than a crash.
 *
 * Selections are built as explicit `Range` objects rather than by simulating a
 * drag. jsdom does not lay text out, so there is no geometry to drag across —
 * but a Range is exactly what the browser hands `window.getSelection()` anyway,
 * so building one directly tests the same input the real code receives, with
 * the node and offset pair stated rather than inferred.
 */

import { describe, expect, it } from 'vitest'
import { cleanup, render } from '@testing-library/react'
import { SpanText, readSelectionRange } from './Primitives'

const CONTENT =
  'T2DM with suboptimal control. HbA1c 8.4%. Query early microalbuminuria.'

/** Render SpanText and hand back the container the component code reads from. */
function renderContent(emphasis = null) {
  const { container } = render(
    <div data-testid="content">
      <SpanText content={CONTENT} emphasis={emphasis} />
    </div>
  )
  return container.querySelector('[data-testid="content"]')
}

/** Select from (anchorNode, anchorOffset) to (focusNode, focusOffset). */
function select(anchorNode, anchorOffset, focusNode, focusOffset) {
  const range = document.createRange()
  range.setStart(anchorNode, anchorOffset)
  range.setEnd(focusNode, focusOffset)
  const selection = window.getSelection()
  selection.removeAllRanges()
  selection.addRange(range)
}

/** The nth text node inside `root`, in document order. */
function textNodes(root) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  const found = []
  let node
  while ((node = walker.nextNode())) found.push(node)
  return found
}

describe('readSelectionRange — text-node selections', () => {
  it('reads a mid-sentence selection as character offsets into the content', () => {
    const container = renderContent()
    const [text] = textNodes(container)

    // "HbA1c 8.4%" — offsets 30..40 in CONTENT.
    select(text, 30, text, 40)

    expect(readSelectionRange(container)).toEqual({ start: 30, end: 40 })
    expect(CONTENT.slice(30, 40)).toBe('HbA1c 8.4%')
  })

  it('normalises a backwards selection', () => {
    const container = renderContent()
    const [text] = textNodes(container)

    // Dragging right-to-left puts the anchor after the focus.
    select(text, 30, text, 40)
    window.getSelection().setBaseAndExtent(text, 40, text, 30)

    expect(readSelectionRange(container)).toEqual({ start: 30, end: 40 })
  })

  it('refuses a selection shorter than the minimum span', () => {
    const container = renderContent()
    const [text] = textNodes(container)

    select(text, 5, text, 7) // two characters
    expect(readSelectionRange(container)).toBeNull()
  })

  it('refuses a collapsed selection', () => {
    const container = renderContent()
    const [text] = textNodes(container)

    select(text, 12, text, 12)
    expect(readSelectionRange(container)).toBeNull()
  })

  it('refuses a selection outside the entry it was asked about', () => {
    const container = renderContent()
    const outside = document.createElement('p')
    outside.textContent = 'Some other note entirely.'
    document.body.appendChild(outside)

    const [text] = textNodes(outside)
    select(text, 0, text, 10)

    expect(readSelectionRange(container)).toBeNull()
    outside.remove()
  })
})

describe('readSelectionRange — across an emphasised span', () => {
  /**
   * After a provenance click-through, SpanText splits the content into three
   * nodes so it can mark the cited range. Offsets have to keep meaning the same
   * thing across that split, or highlighting an already-cited entry lands
   * somewhere else.
   */
  const EMPHASIS = { start: 30, end: 40 }

  it('reads an offset inside the trailing segment correctly', () => {
    const container = renderContent(EMPHASIS)
    const nodes = textNodes(container)
    expect(nodes).toHaveLength(3) // before, marked, after

    const trailing = nodes[2]
    // 8 characters into the text that follows the mark: 40 + 8.
    select(trailing, 8, trailing, 20)

    expect(readSelectionRange(container)).toEqual({ start: 48, end: 60 })
  })

  it('reads a selection spanning the mark boundary correctly', () => {
    const container = renderContent(EMPHASIS)
    const [leading, marked] = textNodes(container)

    select(leading, 25, marked, 5) // 25 -> 30 + 5

    expect(readSelectionRange(container)).toEqual({ start: 25, end: 35 })
  })

  it('agrees with the unsplit rendering for the same visual selection', () => {
    // The same words, selected once before a citation is open and once after,
    // must produce the same offsets. This is the invariant the data-start
    // bookkeeping exists to hold.
    const plain = renderContent()
    const [plainText] = textNodes(plain)
    select(plainText, 42, plainText, 60)
    const before = readSelectionRange(plain)

    // testing-library's unmount, rather than clearing document.body by
    // assigning to its raw-HTML property. That pattern is banned across the
    // whole frontend tree by test_frontend_never_renders_raw_html, which scans
    // file text and rightly does not care that an occurrence is in a test or in
    // a comment. A scanner with a test-shaped hole in it is not a scanner.
    cleanup()

    const split = renderContent(EMPHASIS)
    const nodes = textNodes(split)
    select(nodes[2], 2, nodes[2], 20) // 40 + 2 .. 40 + 20
    const after = readSelectionRange(split)

    expect(after).toEqual(before)
  })
})

describe('readSelectionRange — element-node boundaries (the D-059 pass defect)', () => {
  /**
   * A browser reports a selection anchored on an ELEMENT with an offset that is
   * a child index, not a character offset. Triple-clicking a paragraph, or
   * dragging past the last character, produces exactly this. The old code read
   * that index as a character position, so the highlight landed a few
   * characters into the entry instead of on the selected words — created
   * successfully, and wrong.
   */

  it('resolves a whole-paragraph selection to the whole content', () => {
    const container = renderContent()
    const paragraph = container.querySelector('p')

    // What a triple-click reports: the element, offset 0 to childCount.
    select(paragraph, 0, paragraph, paragraph.childNodes.length)

    expect(readSelectionRange(container)).toEqual({
      start: 0,
      end: CONTENT.length,
    })
  })

  it('resolves a whole-paragraph selection when the content is split by a mark', () => {
    const container = renderContent({ start: 30, end: 40 })
    const paragraph = container.querySelector('p')

    select(paragraph, 0, paragraph, paragraph.childNodes.length)

    expect(readSelectionRange(container)).toEqual({
      start: 0,
      end: CONTENT.length,
    })
  })

  it('resolves an element boundary to the start of the child it points at', () => {
    const container = renderContent({ start: 30, end: 40 })
    const paragraph = container.querySelector('p')

    // From the paragraph start to the boundary before the third child, which
    // begins where the mark ends.
    select(paragraph, 0, paragraph, 2)

    expect(readSelectionRange(container)).toEqual({ start: 0, end: 40 })
  })

  it('never reports an offset past the end of the content', () => {
    const container = renderContent()
    const paragraph = container.querySelector('p')

    select(paragraph, 0, paragraph, paragraph.childNodes.length)
    const range = readSelectionRange(container)

    expect(range.end).toBeLessThanOrEqual(CONTENT.length)
    expect(CONTENT.slice(range.start, range.end)).toBe(CONTENT)
  })
})
