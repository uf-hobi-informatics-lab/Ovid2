---
title: Appearance
summary: Themes, accent colours, fonts and saved looks — stored per user on the server.
audience: [user]
---

Open the **Appearance** panel from the paint-palette button on the right
rail.

## Themes

The panel ships several themes: **light** and **dark** basics, plus
character themes with their own fonts and effects (*clinical*,
*financial*, *matrix*, *spaceship*, *tatooine*, *lego*, *super mario
world*). The day/night button in the top bar switches between light and
dark.

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

> [!TIP]
> If the assistant is enabled, you can just ask it: *"switch to the dark
> theme with a teal accent and save it as night shift"*. It applies the
> change in your browser and offers **Undo**.

The manual follows your chosen appearance too.
