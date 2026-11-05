"""The offer letter (LOI), made from Seth's master Google Doc.

- fields.py: which {{tokens}} a template holds, what fills each one (config/loi.yaml), and the field review.
- auth.py: Google sign-in on this computer (OAuth, token kept in data/, never printed).
- drive.py: the Google Drive and Docs calls (copy, fill, PDF). fake.py: the same calls in memory, for tests.
- service.py: the settings, the template check and making a letter for an offer.

The app never writes letter wording or deal terms: the words come from the master document.
"""
