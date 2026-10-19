"""BAP-5: bounded, SSRF-safe research of a client's public website.

``research.research_website`` fetches the homepage and a few relevant internal pages, extracts
the business content, removes boilerplate and duplicates, and returns verified facts (quoted from
the site, with their source URL) kept apart from interpretations (signals to validate).
"""
