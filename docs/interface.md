# The interface

The look is Apple glass: translucent layered surfaces, hairline borders, depth
from light rather than from boxes. Both light and dark are first-class and
follow the operating system.

Three rules keep it honest for a product where people approve irreversible
changes to their company's data.

## 1. Glass never reduces legibility

Blur sits *behind* content, never through it. Every text colour is checked
against the material it lands on, not against the page background it can no
longer see.

Glass in light mode is not dark mode with inverted tokens: the material is
**lighter** than its backdrop rather than darker, and shadow carries the depth
because a hairline on white is nearly invisible.

Two fallbacks, both real:

- **`prefers-contrast: more`** turns the materials opaque and the hairlines
  into real borders. Blur is a decoration; contrast is not.
- **No `backdrop-filter` support** makes the materials near-opaque. Translucency
  alone would leave text sitting over a gradient.

### A bug worth recording

The ambient field was first implemented as a `z-index: -1` pseudo-element. That
renders *behind* the body's own background colour, so the gradients were
invisible and every "glass" surface was a flat translucent rectangle — code
that looks correct and produces a completely flat interface.

It was found by rendering the page and looking at it, not by reading the CSS.
The field is now the body's own `background-image`.

## 2. Risk is never decorative

`LOW` / `MEDIUM` / `HIGH` / `CRITICAL` differ by **weight and shape as well as
hue**. `CRITICAL` is filled rather than outlined and carries a halo; so does
`PRODUCTION`.

A colour-only signal fails for roughly 8% of men, and approving a `CRITICAL`
change while reading it as `MEDIUM` is the single worst outcome this interface
can produce.

The same applies to execution status, where the shape is the signal:

| State | Shape |
| --- | --- |
| Running | pulsing circle |
| Succeeded | solid circle |
| Failed | **square** |
| Blocked by policy | **diamond** |
| Waiting for approval | **hollow ring** |

The approval card is lifted above everything else on the page with the deepest
shadow in the system. A decision that cannot be undone should not look like
another row in a list.

## 3. Motion respects the person

Every transition is short and eased. All of it is switched off under
`prefers-reduced-motion`.

## Environment is always visible

Which environment a change lands in is the most consequential fact on the
screen, so the topbar always carries it and `PRODUCTION` is filled rather than
outlined.

The connections list shows the **declared** environment rather than the
Salesforce sandbox flag — a sandbox can legitimately be a team's UAT — and when
the two disagree it says so, because production controls apply regardless of the
label.

## Details

- **A visible focus ring everywhere.** Glass has low-contrast edges, so a
  keyboard user needs it more here, not less.
- **Wide tables scroll inside their own surface.** The page never scrolls
  sideways; a horizontal page scrollbar makes a whole application feel broken
  when the problem is one table with too many columns. Applied to the card
  rather than a wrapper element, so it holds for tables added later.
- **The sidebar collapses below 900px** rather than squeezing. A 200px nav with
  truncated labels helps nobody.
- **The identity block truncates** rather than wrapping into the sign-out
  control — a long company name should not push the button off the surface.
- **`themeColor` is set per scheme**, so the browser chrome does not cut a hard
  edge against the page. On a translucent interface that seam is the first
  thing that reads as "web app pretending to be native".
