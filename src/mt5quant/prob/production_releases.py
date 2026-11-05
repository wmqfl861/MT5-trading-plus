"""Fixed T121 trust data. Uninitialized; no production release is approved.

Only independently reviewed registration commits may update this literal.
The adapter verifies Git provenance and parses this data without executing it.
"""
SNAPSHOT_JSON = '{"schema":"t121-trust-v1","initialized":false,"version":1,"valid_from":null,"expires_at":null,"registry":{},"states":{},"context":"production"}'
