import sys
from typing import List, Optional

import torch
from torch import nn

from .attention import (
    ForwardContext,
    get_forward_context,
    reset_forward_context,
    set_forward_context,
)
from .kv_manager import KVCacheManager, Seq


class Sampler(nn.Module):
    def __init__(self):
        super().__init__()

    @torch.compile
    def forward(self, logits: torch.Tensor, temperatures: torch.Tensor):
        logits = logits.float().div_(temperatures.unsqueeze(dim=1))
        probs = torch.softmax(logits, dim=-1)
        sample_tokens = probs.div_(
            torch.empty_like(probs).exponential_(1).clamp_min_(1e-10)
        ).argmax(dim=-1)
        return sample_tokens


class AccelInferenceEngine:
    def __init__(
        self,
        model,
        lm_head,
        num_layers: int,
        num_heads: int,
        head_dim: int,
        block_size: int = 256,
        num_blocks: int = 128,
        use_cuda_graph: bool = True,
    ):
        """
        Args:
            model: The GPT transformer model (should have accel attention)
            lm_head: Language model head for generating logits
            num_layers: Number of transformer layers
            num_heads: Number of attention heads
            head_dim: Dimension per head
            block_size: KV cache block size
            num_blocks: Total number of KV cache blocks
            use_cuda_graph: Whether to use CUDA Graph for decode optimization
        """
        self.model = model
        self.lm_head = lm_head
        self.block_size = block_size
        self.num_blocks = num_blocks
        self.use_cuda_graph = use_cuda_graph and torch.cuda.is_available()
        model_dtype = next(model.parameters()).dtype
        self.hidden_size = (
            model.config.hidden_size
            if hasattr(model, "config")
            else head_dim * num_heads
        )
        self.kv_manager = KVCacheManager(
            num_layers=num_layers,
            num_heads=num_heads,
            head_dim=head_dim,
            block_size=block_size,
            num_blocks=num_blocks,
            dtype=torch.float16,  # Force fp16 for FlashAttention
        )
        self.kv_manager.wire_kv_cache_to_model(model)
        self.sampler = Sampler()
        self.current_sequences = []
        self.graphs = {}
        self.graph_vars = None
        self.graph_pool = None
        self.graph_captured = False

    def _prepare_prefill(self, requests: List[Seq]):
        input_ids = []
        positions = []
        cu_seqlens_q = [0]
        cu_seqlens_k = [0]
        max_seqlen_q = 0
        max_seqlen_k = 0
        slot_mapping = []

        for req in requests:
            seqlen = len(req)
            input_ids.extend(req[req.num_cached_tokens :])
            positions.extend(list(range(req.num_cached_tokens, seqlen)))
            seqlen_q = seqlen - req.num_cached_tokens
            seqlen_k = seqlen
            cu_seqlens_q.append(cu_seqlens_q[-1] + seqlen_q)
            cu_seqlens_k.append(cu_seqlens_k[-1] + seqlen_k)
            max_seqlen_q = max(seqlen_q, max_seqlen_q)
            max_seqlen_k = max(seqlen_k, max_seqlen_k)

            if req.block_table:
                for i in range(req.num_cached_blocks, req.num_blocks):
                    block_id = req.block_table[i]
                    start = block_id * self.block_size
                    if i != req.num_blocks - 1:
                        end = start + self.block_size
                    else:
                        end = start + req.last_block_num_tokens
                    slot_mapping.extend(list(range(start, end)))

        input_ids = torch.tensor(input_ids, dtype=torch.int64, pin_memory=True).cuda(
            non_blocking=True
        )
        positions = torch.tensor(positions, dtype=torch.int64, pin_memory=True).cuda(
            non_blocking=True
        )
        cu_seqlens_q = torch.tensor(
            cu_seqlens_q, dtype=torch.int32, pin_memory=True
        ).cuda(non_blocking=True)
        cu_seqlens_k = torch.tensor(
            cu_seqlens_k, dtype=torch.int32, pin_memory=True
        ).cuda(non_blocking=True)
        slot_mapping = torch.tensor(
            slot_mapping, dtype=torch.int32, pin_memory=True
        ).cuda(non_blocking=True)

        block_tables = None
        if cu_seqlens_k[-1] > cu_seqlens_q[-1]:
            max_len = max(len(req.block_table) for req in requests)
            block_tables_list = []
            for req in requests:
                table = req.block_table + [-1] * (max_len - len(req.block_table))
                block_tables_list.append(table)
            block_tables = torch.tensor(
                block_tables_list, dtype=torch.int32, pin_memory=True
            ).cuda(non_blocking=True)

        set_forward_context(
            True,
            cu_seqlens_q,
            cu_seqlens_k,
            max_seqlen_q,
            max_seqlen_k,
            slot_mapping,
            None,
            block_tables,
        )

        return input_ids, positions

    def _prepare_decode(self, requests: List[Seq]):
        if not requests:
            raise RuntimeError("FATAL: No requests provided to _prepare_decode!")

        input_ids = []
        positions = []
        slot_mapping = []
        context_lens = []

        for req in requests:
            input_ids.append(req.last_token)

            pos = len(req) - 1
            if hasattr(self, "_tts_mode") and self._tts_mode:
                pos = pos - (self._tts_prompt_len - 1)
            positions.append(pos)

            context_lens.append(len(req))
            slot_mapping.append(
                req.block_table[-1] * self.block_size + req.last_block_num_tokens - 1
            )

        input_ids = torch.tensor(input_ids, dtype=torch.int64, pin_memory=True).cuda(
            non_blocking=True
        )
        positions = torch.tensor(positions, dtype=torch.int64, pin_memory=True).cuda(
            non_blocking=True
        )
        slot_mapping = torch.tensor(
            slot_mapping, dtype=torch.int32, pin_memory=True
        ).cuda(non_blocking=True)
        context_lens = torch.tensor(
            context_lens, dtype=torch.int32, pin_memory=True
        ).cuda(non_blocking=True)

        max_len = max(len(req.block_table) for req in requests)
        block_tables_list = []
        for req in requests:
            table = req.block_table + [-1] * (max_len - len(req.block_table))
            block_tables_list.append(table)
        block_tables = torch.tensor(
            block_tables_list, dtype=torch.int32, pin_memory=True
        ).cuda(non_blocking=True)

        assert block_tables.dim() == 2, (
            f"block_tables must be 2D, got shape {block_tables.shape}"
        )
        assert block_tables.size(0) == len(requests), (
            f"block_tables batch size mismatch: {block_tables.size(0)} vs {len(requests)}"
        )

        set_forward_context(
            False,
            slot_mapping=slot_mapping,
            context_lens=context_lens,
            block_tables=block_tables,
        )

        return input_ids, positions

    def _prepare_sample(self, requests: List[Seq], temperature: float):
        temperatures = [temperature] * len(requests)
        temperatures = torch.tensor(
            temperatures, dtype=torch.float32, pin_memory=True
        ).cuda(non_blocking=True)
        return temperatures

    @torch.inference_mode()
    def _capture_cuda_graphs(self, tts_mel_embedding=None, tts_text_pos_embedding=None):
        print("Capturing CUDA graphs for decode optimization...")
        max_bs = 8  # Support up to batch size 8
        max_num_blocks = (2048 + self.block_size - 1) // self.block_size
        model_dtype = next(self.model.parameters()).dtype
        input_ids = torch.ones(max_bs, dtype=torch.int64, device="cuda") * 8192
        positions = torch.ones(max_bs, dtype=torch.int64, device="cuda")
        slot_mapping = torch.zeros(max_bs, dtype=torch.int32, device="cuda")
        context_lens = torch.zeros(max_bs, dtype=torch.int32, device="cuda")
        block_tables = torch.zeros(
            max_bs, max_num_blocks, dtype=torch.int32, device="cuda"
        )
        outputs = torch.zeros(
            max_bs, self.hidden_size, dtype=model_dtype, device="cuda"
        )
        inputs_embeds_buffer = torch.zeros(
            max_bs, self.hidden_size, dtype=model_dtype, device="cuda"
        )

        self.graph_bs = [1]

        use_tts = tts_mel_embedding is not None and tts_text_pos_embedding is not None

        for bs in reversed(self.graph_bs):
            graph = torch.cuda.CUDAGraph()

            slot_mapping[:bs] = torch.arange(bs, dtype=torch.int32, device="cuda")
            context_lens[:bs] = bs + 1
            block_tables[:bs, 0] = 0

            set_forward_context(
                False,
                slot_mapping=slot_mapping[:bs],
                context_lens=context_lens[:bs],
                block_tables=block_tables[:bs],
            )

            # warmup
            if use_tts:
                assert tts_mel_embedding is not None
                assert tts_text_pos_embedding is not None
                emb = tts_mel_embedding(input_ids[:bs])
                pos_clamped = torch.clamp(positions[:bs], min=0)
                pos_emb = tts_text_pos_embedding.emb(pos_clamped)
                inputs_embeds_buffer[:bs] = emb + pos_emb
                out = self.model(
                    inputs_embeds=inputs_embeds_buffer[:bs].unsqueeze(1),
                    return_dict=True,
                ).last_hidden_state
            else:
                out = self.model(
                    input_ids=input_ids[:bs].unsqueeze(1), return_dict=True
                ).last_hidden_state
            outputs[:bs] = out.squeeze(1) if out.dim() == 3 else out

            with torch.cuda.graph(graph, self.graph_pool):
                if use_tts:
                    assert tts_mel_embedding is not None
                    assert tts_text_pos_embedding is not None
                    emb = tts_mel_embedding(input_ids[:bs])
                    pos_clamped = torch.clamp(positions[:bs], min=0)
                    pos_emb = tts_text_pos_embedding.emb(pos_clamped)
                    inputs_embeds_buffer[:bs] = emb + pos_emb
                    out = self.model(
                        inputs_embeds=inputs_embeds_buffer[:bs].unsqueeze(1),
                        return_dict=True,
                    ).last_hidden_state
                else:
                    out = self.model(
                        input_ids=input_ids[:bs].unsqueeze(1), return_dict=True
                    ).last_hidden_state
                outputs[:bs] = out.squeeze(1) if out.dim() == 3 else out

            if self.graph_pool is None:
                self.graph_pool = graph.pool()

            self.graphs[bs] = graph
            torch.cuda.synchronize()
            reset_forward_context()

        self.graph_vars = {
            "input_ids": input_ids,
            "positions": positions,
            "slot_mapping": slot_mapping,
            "context_lens": context_lens,
            "block_tables": block_tables,
            "outputs": outputs,
            "inputs_embeds": inputs_embeds_buffer,
        }
        print(f"CUDA graphs captured for batch sizes: {self.graph_bs}")

    @torch.inference_mode()
    def _run_decode_with_graph(
        self,
        input_ids: torch.Tensor,
        positions: torch.Tensor,
        context: ForwardContext,
        tts_mel_embedding: Optional[torch.nn.Module] = None,
        tts_text_pos_embedding: Optional[torch.nn.Module] = None,
    ) -> torch.Tensor:
        bs = input_ids.size(0)
        use_tts_embedding = hasattr(self, "_tts_mode") and self._tts_mode

        if not self.use_cuda_graph or not self.graphs:
            if use_tts_embedding:
                assert tts_mel_embedding is not None
                assert tts_text_pos_embedding is not None
                inputs_embeds = tts_mel_embedding(input_ids)
                pos_clamped = torch.clamp(positions, min=0)
                pos_emb = tts_text_pos_embedding.emb(pos_clamped)
                inputs_embeds = inputs_embeds + pos_emb
                out = self.model(
                    inputs_embeds=inputs_embeds.unsqueeze(1), return_dict=True
                ).last_hidden_state
            else:
                out = self.model(
                    input_ids=input_ids.unsqueeze(1), return_dict=True
                ).last_hidden_state
            return out.squeeze(1) if out.dim() == 3 else out

        graph_bs = next((x for x in self.graph_bs if x >= bs), None)
        if graph_bs is None:
            if use_tts_embedding:
                assert tts_mel_embedding is not None
                assert tts_text_pos_embedding is not None
                inputs_embeds = tts_mel_embedding(input_ids)
                pos_clamped = torch.clamp(positions, min=0)
                pos_emb = tts_text_pos_embedding.emb(pos_clamped)
                inputs_embeds = inputs_embeds + pos_emb
                out = self.model(
                    inputs_embeds=inputs_embeds.unsqueeze(1), return_dict=True
                ).last_hidden_state
            else:
                out = self.model(
                    input_ids=input_ids.unsqueeze(1), return_dict=True
                ).last_hidden_state
            return out.squeeze(1) if out.dim() == 3 else out

        graph = self.graphs[graph_bs]
        graph_vars = self.graph_vars

        if graph_vars is None:
            raise RuntimeError("Graph variables not initialized")

        set_forward_context(
            False,
            slot_mapping=graph_vars["slot_mapping"][:graph_bs],
            context_lens=graph_vars["context_lens"][:graph_bs],
            block_tables=graph_vars["block_tables"][:graph_bs],
        )

        graph_vars["input_ids"][:bs] = input_ids
        graph_vars["positions"][:bs] = positions
        graph_vars["slot_mapping"].fill_(-1)
        graph_vars["slot_mapping"][:bs] = context.slot_mapping
        graph_vars["context_lens"].zero_()
        graph_vars["context_lens"][:bs] = context.context_lens
        graph_vars["block_tables"][:bs, : context.block_tables.size(1)] = (
            context.block_tables
        )
        graph.replay()

        return graph_vars["outputs"][:bs]

    @torch.inference_mode()
    def generate(
        self,
        input_ids: torch.Tensor,
        max_new_tokens: int = 100,
        temperature: float = 1.0,
        top_k: int = 50,
        top_p: float = 1.0,
        stop_tokens: Optional[List[int]] = None,
        attention_mask: Optional[torch.Tensor] = None,
        tts_embeddings: Optional[
            torch.Tensor
        ] = None,  # TTS: [pad][cond][text] embeddings (87 tokens, NO start_mel)
        tts_mel_embedding: Optional[torch.nn.Module] = None,  # TTS: mel_embedding layer
        tts_text_pos_embedding: Optional[
            torch.nn.Module
        ] = None,  # TTS: text_pos_embedding layer
    ) -> torch.Tensor:
        """
        Generate tokens.

        Args:
            input_ids: Input token IDs [batch_size, seq_len]
            max_new_tokens: Maximum number of tokens to generate
            temperature: Sampling temperature
            top_k: Top-k sampling
            top_p: Nucleus sampling threshold
            stop_tokens: List of token IDs that stop generation

        Returns:
            Generated token IDs [batch_size, total_len]
        """
        batch_size = input_ids.size(0)
        device = input_ids.device

        self._tts_mode = tts_embeddings is not None
        self._tts_prompt_len = input_ids.size(1) if self._tts_mode else 0

        if self.use_cuda_graph and not self.graph_captured:
            print(
                f"[CAPTURE] use_cuda_graph={self.use_cuda_graph}, graph_captured={self.graph_captured}",
                file=sys.stderr,
                flush=True,
            )
            self._capture_cuda_graphs(
                tts_mel_embedding=tts_mel_embedding,
                tts_text_pos_embedding=tts_text_pos_embedding,
            )
            self.graph_captured = True
            print(
                f"[CAPTURE] Completed! graphs={list(self.graphs.keys())}",
                file=sys.stderr,
                flush=True,
            )

        if tts_embeddings is not None:
            actual_seq_len = tts_embeddings.size(1) + 1  # embeddings + start_mel_token
            pass
        else:
            actual_seq_len = input_ids.size(1)

        sequences = []
        for i in range(batch_size):
            token_ids = [1] * actual_seq_len
            if tts_embeddings is not None and actual_seq_len > 0:
                token_ids[-1] = input_ids[i, -1].item() if input_ids.size(1) > 0 else 1
            else:
                token_ids = input_ids[i].tolist()
            req = Seq(token_ids)
            self.kv_manager.allocate(req)
            sequences.append(req)

        self.current_sequences = sequences

        # Prefill phase
        prefill_ids, prefill_pos = self._prepare_prefill(sequences)

        if prefill_ids.dim() == 1:
            prefill_ids = prefill_ids.unsqueeze(
                0
            )  # [total_tokens] -> [1, total_tokens]
        if prefill_pos.dim() == 1:
            prefill_pos = prefill_pos.unsqueeze(
                0
            )  # [total_tokens] -> [1, total_tokens]

        if (
            tts_embeddings is not None
            and tts_mel_embedding is not None
            and tts_text_pos_embedding is not None
        ):
            start_token_id = input_ids[0, -1] if input_ids.size(1) > 0 else 8192

            start_emb = tts_mel_embedding(
                torch.tensor([[start_token_id]], device="cuda")
            )  # [1, 1, hidden_dim]

            start_emb = start_emb + tts_text_pos_embedding(start_emb)

            full_embeddings = torch.cat(
                [tts_embeddings, start_emb], dim=1
            )  # [1, 88, hidden_dim]

            model_dtype = next(self.model.parameters()).dtype
            if full_embeddings.dtype != model_dtype:
                full_embeddings = full_embeddings.to(model_dtype)

            hidden_states = self.model(
                inputs_embeds=full_embeddings, return_dict=True
            ).last_hidden_state

        else:
            hidden_states = self.model(
                input_ids=input_ids, attention_mask=attention_mask, return_dict=True
            ).last_hidden_state

        reset_forward_context()

        last_hidden = hidden_states[:, -1, :]  # [batch_size, hidden_size]

        if self.lm_head is not None:
            if last_hidden.dtype != next(self.lm_head.parameters()).dtype:
                last_hidden = last_hidden.to(next(self.lm_head.parameters()).dtype)
            logits = self.lm_head(last_hidden)  # [batch_size, vocab_size]
        else:
            logits = self.model.compute_logits(last_hidden)  # [batch_size, vocab_size]

        temperatures = self._prepare_sample(sequences, temperature)
        if temperature > 0:
            first_token = self.sampler(logits, temperatures)
        else:
            first_token = torch.argmax(logits, dim=-1)

        first_token_list = first_token.tolist()

        generated_tokens = [[] for _ in range(batch_size)]
        hit_stop_on_first = False

        for i, token_id in enumerate(first_token_list):
            if stop_tokens and token_id in stop_tokens:
                hit_stop_on_first = True
            else:
                generated_tokens[i].append(token_id)

        if hit_stop_on_first:
            for req in sequences:
                self.kv_manager.remove_seq(req)
            self.current_sequences = []

            output_ids = []
            for i in range(batch_size):
                full_sequence = input_ids[i].tolist() + generated_tokens[i]
                output_ids.append(full_sequence)

            output = torch.tensor(output_ids, dtype=torch.long, device=device)
            return output

        if not hit_stop_on_first:
            for i, req in enumerate(sequences):
                req.append_token(first_token_list[i])
                self.kv_manager.append_to_seq(req)

        remaining_tokens = max_new_tokens - 1

        for step in range(remaining_tokens):
            decode_ids, decode_pos = self._prepare_decode(sequences)

            # Forward pass
            if batch_size > 8:
                raise RuntimeError(
                    f"FATAL: batch_size={batch_size} exceeds CUDA Graph limit (8)!"
                )

            context = get_forward_context()
            hidden_states = self._run_decode_with_graph(
                decode_ids,
                decode_pos,
                context,
                tts_mel_embedding=tts_mel_embedding,
                tts_text_pos_embedding=tts_text_pos_embedding,
            )

            # Get logits
            if self.lm_head is not None:
                logits = self.lm_head(hidden_states)  # [batch_size, vocab_size]
            else:
                logits = self.model.compute_logits(
                    hidden_states
                )  # [batch_size, vocab_size]

            reset_forward_context()

            temperatures = self._prepare_sample(sequences, temperature)
            if temperature > 0:
                next_token = self.sampler(logits, temperatures)
            else:
                next_token = torch.argmax(logits, dim=-1)
            next_token_list = next_token.tolist()

            should_stop = False
            for i, token_id in enumerate(next_token_list):
                if stop_tokens and token_id in stop_tokens:
                    should_stop = True
                else:
                    sequences[i].append_token(token_id)
                    self.kv_manager.append_to_seq(sequences[i])
                    generated_tokens[i].append(token_id)

            if should_stop:
                break

        for req in sequences:
            self.kv_manager.remove_seq(req)
        self.current_sequences = []

        output_ids = []
        for i in range(batch_size):
            initial_tokens = sequences[i].token_ids[: sequences[i].num_prompt_tokens]
            full_sequence = initial_tokens + generated_tokens[i]
            output_ids.append(full_sequence)

        output = torch.tensor(output_ids, dtype=torch.long, device=device)

        assert output.size(0) == batch_size, (
            f"Output batch size mismatch: {output.size(0)} != {batch_size}"
        )

        return output
