---
title: Appearance
summary: Themes, accent colours, fonts and saved looks — stored per user on the server.
audience: [user]
---

Open the **Appearance** panel from the paint-palette button on the right
rail. The assistant, Appearance and search (Ctrl-K) panels all open from
the right edge at the same width; opening one closes the others.

## Themes

The panel ships eight themes, listed alphabetically, each with its own
colours, fonts and small effects:

| Theme | Mode | Feel |
| --- | --- | --- |
| *Clinical* | light | calm hospital teal, IBM Plex |
| *Data Factory* (default) | light | a working factory floor: concrete, a safety-yellow gantry, conveyor belts that move while a job runs, stencilled titles, hazard tape, QC stamps |
| *Dune* | light | the deep desert at noon: bleached sand, basalt menu, electric-blue accent, carved capitals, wind ripples and thumper pulses |
| *Financial* | dark | amber trading terminal, dense tables, prompt-marked menu |
| *LEGO* | light | toy bricks |
| *Matrix* | dark | phosphor-green digital rain |
| *Spaceship* | dark | sci-fi console, teal glow |
| *Super Mario World* | light | 16-bit sky and bricks |

Until you pick a theme, the panel uses **Data Factory**. A look saved with
the retired *Star Wars / Tatooine* theme now opens in *Dune*.

## Customise

On top of any theme you can override:

- the accent and surface **colours**;
- body and heading **fonts**, sizes and weights;
- per-component styles: navigation, buttons, cards, tables.

Overrides are validated. A change that would make body text unreadable
(contrast below 3:1) is refused.

## Saved looks

Type a name under **Your themes** and click **Save** to keep the current
theme and overrides. Saved looks appear in the same theme list as the
built-in themes (alphabetically, with their own colour swatch and the
theme they are based on); click one to apply it, or **×** to delete it.

## Where it is stored

Appearance is stored **per user, on the server**
(`/api/v1/me/appearance`), not in the browser. Your look follows you to
any browser or computer, and it is painted before the page renders, so
there is no flash of the wrong theme.

When sign-in is turned off (the default on a fresh install), every
visitor is the same `anonymous` user. The whole deployment then shares
**one** appearance profile: a change anyone makes applies to everyone.
Turn on [sign-in](../08-operations/auth.md) to give each user their own.

> [!TIP]
> If the assistant is enabled, you can just ask it: *"switch to the dark
> theme with a teal accent and save it as night shift"*. It applies the
> change in your browser and offers **Undo**.

The manual follows your chosen appearance too.
