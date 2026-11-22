---
title: Appearance
summary: Themes, accent colours, fonts and saved looks — stored per user on the server.
audience: [user]
---

Open the **Appearance** panel from the paint-palette button on the right
rail.

## Themes

The panel ships eight themes, each with its own colours, fonts and small
effects:

| Theme | Mode | Feel |
| --- | --- | --- |
| *Data Factory* (default) | light | industrial line control: steel grey, a dark machine-side menu, drive-blue accent, condensed telemetry type |
| *Clinical* | light | calm hospital teal, IBM Plex |
| *LEGO* | light | toy bricks |
| *Super Mario World* | light | 16-bit sky and bricks |
| *Matrix* | dark | phosphor-green digital rain |
| *Financial* | dark | amber trading terminal, dense tables |
| *Spaceship* | dark | sci-fi console, teal glow |
| *Star Wars / Tatooine* | dark | desert dusk, cut-corner panels |

Until you pick a theme, the panel uses **Data Factory**.

## Customise

On top of any theme you can override:

- the accent and surface **colours**;
- body and heading **fonts**, sizes and weights;
- per-component styles: navigation, buttons, cards, tables.

Overrides are validated. A change that would make body text unreadable
(contrast below 3:1) is refused.

## Saved looks

Click **Save** to name the current theme and overrides, then re-apply them
later from the list.

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
