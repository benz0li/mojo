# ===----------------------------------------------------------------------=== #
# Copyright (c) 2025, Modular Inc. All rights reserved.
#
# Licensed under the Apache License v2.0 with LLVM Exceptions:
# https://llvm.org/LICENSE.txt
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ===----------------------------------------------------------------------=== #

"""Standardized context object for Pipeline Inference."""

from __future__ import annotations

from typing import Any, Optional, Protocol, Sequence, Union, runtime_checkable

import numpy as np

from .interfaces import LogProbabilities

CHUNK_SIZE = 128


@runtime_checkable
class InputContext(Protocol):
    """A base class for model contexts, represent model inputs for TokenGenerators."""

    @property
    def cache_seq_id(self) -> int: ...

    @property
    def active_idx(self) -> int: ...

    @property
    def start_idx(self) -> int: ...

    @property
    def end_idx(self) -> int: ...

    @property
    def current_length(self) -> int:
        """The current length of the sequence, including completed and active tokens."""
        ...

    @property
    def max_length(self) -> int | None:
        """The maximum length of this sequence."""
        ...

    @property
    def log_probabilities(self) -> int:
        """When > 0, returns the log probabilities for the top N tokens for each
        element token in the sequence."""
        ...

    @property
    def log_probabilities_echo(self) -> bool:
        """When True, the input tokens are added to the returned logprobs."""
        ...

    @property
    def active_length(self) -> int:
        """Current sequence length: num tokens input this iteration.

        This will be the prompt size for context encoding, and simply 1 for
        token generation.
        """
        ...

    @property
    def next_tokens(self) -> np.ndarray:
        """The next prompt tokens to be input during this iteration.

        This should be a 1D array of tokens of length active_length.
        """
        ...

    def update(
        self,
        new_token: int,
        log_probabilities: Optional[LogProbabilities] = None,
        is_eos: bool = False,
    ) -> None:
        """Updates the next_tokens and extends existing tokens to include all generated tokens."""
        ...

    def jump_ahead(self, new_token: int) -> None:
        """Updates the token array, while ensuring the new token is returned to the user."""
        ...

    def bump_token_indices(
        self,
        start_idx: Optional[int] = None,
        active_idx: Optional[int] = None,
        end_idx: Optional[int] = None,
    ) -> None:
        """Update the start_idx, active_idx and end_idx without manipulating the token array."""
        ...

    @property
    def matcher(self) -> Optional["xgr.GrammarMatcher"]:  # type: ignore
        """An optional xgr Grammar Matcher provided when using structured output."""
        ...

    @property
    def json_schema(self) -> str | None:
        """A json schema to use during constrained decoding."""
        ...

    def set_matcher(self, matcher: "xgr.GrammarMatcher") -> None:  # type: ignore
        """Set a grammar matcher for use during constrained decoding."""
        ...

    def reset(self) -> None:
        """Resets the context's state by combining all tokens into a new prompt.
        This method is used when a request is evicted, meaning that the context
        needed to be re-encoded in the following CE iteration."""
        ...

    def outstanding_completion_tokens(
        self,
    ) -> list[tuple[int, Optional[LogProbabilities]]]:
        """Return the list of outstanding completion tokens and log probabilities
        that must be returned to the user."""
        ...


class TextContext:
    """A base class for model context, specifically for Text model variants."""

    def __init__(
        self,
        cache_seq_id: int,
        prompt: Union[str, Sequence[int]],
        max_length: int | None,
        tokens: np.ndarray,
        log_probabilities: int = 0,
        log_probabilities_echo: bool = False,
        json_schema: str | None = None,
    ) -> None:
        self.cache_seq_id = cache_seq_id
        self.prompt = prompt
        self.max_length = max_length

        if tokens.ndim != 1:
            msg = f"tokens must be one dimensional array: got shape '{tokens.shape}'"
            raise ValueError(msg)

        self.size = int(np.ceil(len(tokens) / CHUNK_SIZE) * CHUNK_SIZE)

        # Create a fresh array since the input tokens may be a view or share memory with
        # another array in the caller, which prevents us from resizing it directly.
        # The extra space is initialized to zero and will be filled with generated tokens.
        assert len(tokens) <= self.size
        self.tokens = np.zeros(self.size, dtype=tokens.dtype)
        self.tokens[: len(tokens)] = tokens

        self._active_idx = len(tokens)
        self._start_idx = 0
        self._end_idx = self._active_idx
        self._completion_start_idx = self._active_idx
        self._completion_end_idx = self._active_idx

        self.log_probabilities = log_probabilities
        self.log_probabilities_echo = log_probabilities_echo
        self._log_probabilities_data: dict[int, LogProbabilities] = {}

        self.matcher = None
        self.json_schema = json_schema
        self.is_initial_prompt = True

    @property
    def start_idx(self) -> int:
        return self._start_idx

    @property
    def active_idx(self) -> int:
        return self._active_idx

    @property
    def end_idx(self) -> int:
        return self._end_idx

    def set_matcher(self, matcher: "xgr.GrammarMatcher") -> None:  # type: ignore
        self.matcher = matcher

    @property
    def current_length(self) -> int:
        """The current length of the sequence, including completed and active tokens."""
        return self._end_idx

    @property
    def active_length(self) -> int:
        """Current sequence length: num tokens input this iteration.

        This will be the prompt size for context encoding, and simply 1 (or more) for
        token generation.
        """
        return self._active_idx - self._start_idx

    def bump_token_indices(
        self,
        start_idx: Optional[int] = None,
        active_idx: Optional[int] = None,
        end_idx: Optional[int] = None,
    ) -> None:
        """Update the start_idx, active_idx and end_idx without manipulating the token array."""
        new_start_idx = (start_idx if start_idx else 0) + self._start_idx
        new_active_idx = (active_idx if active_idx else 0) + self._active_idx
        new_end_idx = (end_idx if end_idx else 0) + self._end_idx

        if new_start_idx >= new_active_idx:
            msg = f"""
            active_idx must always be greater than start_idx, unable to bump token indices
            as new start_idx ({new_start_idx}) is greater than new active_idx ({new_active_idx}).
            """
            raise ValueError(msg)

        if new_active_idx > new_end_idx:
            msg = f"""
            end_idx must always be greater than active_idx, unable to bump token indices
            as new active_idx ({new_active_idx}) is greater than new end_idx ({new_end_idx}).
            """
            raise ValueError(msg)

        self._start_idx = new_start_idx
        self._active_idx = new_active_idx
        self._end_idx = new_end_idx

    @property
    def next_tokens(self) -> np.ndarray:
        return self.tokens[self._start_idx : self._active_idx]

    def _upsize(self) -> None:
        if self._end_idx >= self.size:
            self.size += CHUNK_SIZE
            self.tokens = np.resize(self.tokens, self.size)

    def update(
        self,
        new_token: int,
        log_probabilities: Optional[LogProbabilities] = None,
        is_eos: bool = False,
    ) -> None:
        """Updates the next_tokens and extends existing tokens to include all generated tokens."""
        # This is required for chunked prefill.
        # The scheduler will update the active_idx via bump_token_indices and pass through the model
        # To accomodate for this, if we identify that the active_idx is not at the end of the completed
        # token array, we only update the start_idx and active_idx, leaving the token array alone.
        if self._active_idx < self._end_idx:
            self._start_idx = self._active_idx
            self._active_idx = self._end_idx
            return

        # Update tokens and log probabilities data
        self._upsize()
        self.tokens[self._active_idx] = new_token
        if log_probabilities:
            self._log_probabilities_data[self._active_idx] = log_probabilities

        # Bump Indices
        self._start_idx = self._active_idx
        self._active_idx += 1
        self._end_idx += 1

        if not is_eos:
            self._completion_end_idx += 1

        # Accept the token, and move the FSM for constrained decoding forward.
        if self.matcher:
            assert self.matcher.accept_token(new_token)

        self.is_initial_prompt = False

    def jump_ahead(self, new_token: int) -> None:
        """Updates the token array, while ensuring the new token is returned to the user."""

        self._upsize()

        # Update tokens
        self.tokens[self._active_idx] = new_token

        # Bump Indices
        self._active_idx += 1
        self._end_idx += 1
        self._completion_end_idx += 1

        # Accept the token, and move the FSM for constrained decoding forward.
        if self.matcher:
            assert self.matcher.accept_token(new_token)

        self.is_initial_prompt = False

    def reset(self) -> None:
        """Resets the context's state by combining all tokens into a new prompt."""
        self._start_idx = 0

        self.is_initial_prompt = True

    def outstanding_completion_tokens(
        self,
    ) -> list[tuple[int, Optional[LogProbabilities]]]:
        """Return the list of outstanding completion tokens and log probabilities
        that must be returned to the user."""
        res = []
        for token_idx in range(
            self._completion_start_idx, self._completion_end_idx
        ):
            # We are using a pop here instead of a get, as we should not have
            # to maintain this data once it is returned. The expectation is that
            # this method never returns the same tokens more than once.
            res.append(
                (
                    self.tokens[token_idx],
                    self._log_probabilities_data.pop(token_idx, None),
                )
            )

        self._completion_start_idx = self._completion_end_idx

        return res


class TextAndVisionContext(TextContext):
    """A base class for model context, specifically for Vision model variants."""

    def __init__(
        self,
        cache_seq_id: int,
        prompt: Union[str, Sequence[int]],
        max_length: int | None,
        tokens: np.ndarray,
        pixel_values: Sequence[np.ndarray],
        extra_model_args: dict[str, Any],
        log_probabilities: int = 0,
        log_probabilities_echo: bool = False,
        json_schema: str | None = None,
    ) -> None:
        super().__init__(
            cache_seq_id=cache_seq_id,
            prompt=prompt,
            max_length=max_length,
            tokens=tokens,
            log_probabilities=log_probabilities,
            log_probabilities_echo=log_probabilities_echo,
            json_schema=json_schema,
        )
        self.pixel_values = pixel_values
        self.extra_model_args = extra_model_args

    def update(
        self,
        new_token: int,
        log_probabilities: Optional[LogProbabilities] = None,
        is_eos: bool = False,
    ) -> None:
        """Updates the next_tokens and extends existing tokens to include all generated tokens."""
        super().update(
            new_token=new_token,
            log_probabilities=log_probabilities,
            is_eos=is_eos,
        )

        # Update context not to re-encode the same image in next steps. There are no image tokens
        # expected after context encoding.
        self.pixel_values = ()
