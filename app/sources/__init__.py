"""Which services answer for an account, and whose answer wins.

A package rather than a module inside calendar/ or auth/ because it is about
neither: it states which services the calendar shows, and whose description of a
title a viewer reads when two of them fill the same field differently. Both
features read it and neither owns it.

IT HAS NO ROUTES OF ITS OWN ANY MORE. It had a screen once, and the screen was
the problem: every question that could only be answered there was a question
almost nobody was asking, and the two that people do ask now live where the rest
of their kind already do — which services show, in the calendar's own filters
panel beside the genre narrowing; which service's description leads, on the
account page beside the tracker's own order.
"""
