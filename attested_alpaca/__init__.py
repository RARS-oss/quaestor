"""attested-alpaca — verifiable execution for any Alpaca agent.

Public surface: wrap your credentials in AttestedAlpaca, place orders that come
back with a signed, hermetically-sealed receipt, and verify any receipt offline.
"""
from attested_alpaca.client import AttestedAlpaca, AttestedOrder, from_env

__all__ = ["AttestedAlpaca", "AttestedOrder", "from_env"]
__version__ = "0.1.0"
