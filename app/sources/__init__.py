"""Which services answer for an account, and whose answer wins.

A package rather than a module inside calendar/ or auth/ because it is about
neither: it states which of the linked services the calendar reads, which of them
the tracker reads, and which one's value a viewer sees when two of them fill the
same field with different things. Both features read it and neither owns it.
"""
