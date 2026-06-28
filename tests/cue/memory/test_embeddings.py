"""tests/cue/memory/test_embeddings.py — Slice 2 embedding provider seam."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.cue.memory.embeddings import embed, EMBED_DIM


def _client_returning(vector):
    client = MagicMock()
    resp = MagicMock()
    item = MagicMock()
    item.embedding = vector
    resp.data = [item]
    client.embeddings.create = AsyncMock(return_value=resp)
    return client


@pytest.mark.asyncio
class TestEmbed:
    async def test_returns_vector(self):
        vec = [0.01] * EMBED_DIM
        with patch("services.cue.memory.embeddings.get_openai_client", return_value=_client_returning(vec)):
            out = await embed("the doctor is preparing the CDMX launch")
        assert out == vec
        assert len(out) == EMBED_DIM

    async def test_none_when_no_client(self):
        with patch("services.cue.memory.embeddings.get_openai_client", return_value=None):
            assert await embed("anything") is None

    async def test_none_on_error(self):
        client = MagicMock()
        client.embeddings.create = AsyncMock(side_effect=RuntimeError("openai down"))
        with patch("services.cue.memory.embeddings.get_openai_client", return_value=client):
            assert await embed("anything") is None

    async def test_none_for_empty_text(self):
        # never call the API for empty input
        with patch("services.cue.memory.embeddings.get_openai_client") as gc:
            assert await embed("") is None
            assert await embed("   ") is None
            gc.assert_not_called()
