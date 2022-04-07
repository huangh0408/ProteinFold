# Author: Yijia Xiao.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import math

import torch
import torch.nn as nn
import torch.nn.functional as F

from .modules import (
    TransformerLayer,
    AxialTransformerLayer,
    LearnedPositionalEmbedding,
    SinusoidalPositionalEmbedding,
    RobertaLMHead,
    ESM1bLayerNorm,
    ContactPredictionHead,
)

from .axial_attention import RowSelfAttention, ColumnSelfAttention


class MegatronMSA(nn.Module):
    @classmethod
    def add_args(cls, parser):
        # fmt: off
        parser.add_argument(
            "--num_layers",
            default=12,
            type=int,
            metavar="N",
            help="number of layers"
        )
        parser.add_argument(
            "--embed_dim",
            default=768,
            type=int,
            metavar="N",
            help="embedding dimension"
        )
        parser.add_argument(
            "--logit_bias",
            action="store_true",
            help="whether to apply bias to logits"
        )
        parser.add_argument(
            "--ffn_embed_dim",
            default=3072,
            type=int,
            metavar="N",
            help="embedding dimension for FFN",
        )
        parser.add_argument(
            "--attention_heads",
            default=12,
            type=int,
            metavar="N",
            help="number of attention heads",
        )
        parser.add_argument(
            "--dropout",
            default=0.1,
            type=float,
            help="Dropout to apply."
        )
        parser.add_argument(
            "--attention_dropout",
            default=0.1,
            type=float,
            help="Dropout to apply."
        )
        parser.add_argument(
            "--activation_dropout",
            default=0.1,
            type=float,
            help="Dropout to apply."
        )
        parser.add_argument(
            "--max_tokens_per_msa",
            default=2 ** 14,
            type=int,
            help=(
                "Used during inference to batch attention computations in a single "
                "forward pass. This allows increased input sizes with less memory."
            ),
        )
        # fmt: on

    def __init__(self, args, alphabet):
        super().__init__()
        self.args = args
        self.alphabet_size = len(alphabet)
        self.padding_idx = alphabet.padding_idx
        self.mask_idx = alphabet.mask_idx
        self.cls_idx = alphabet.cls_idx
        self.eos_idx = alphabet.eos_idx
        self.prepend_bos = alphabet.prepend_bos
        self.append_eos = alphabet.append_eos
        self.embed_tokens = nn.Embedding(
            self.alphabet_size, self.args.hidden_size, padding_idx=self.padding_idx
        )
        self.embed_positions = LearnedPositionalEmbedding(
            self.args.max_position_embeddings,
            self.args.hidden_size,
            self.padding_idx,
        )
        if getattr(self.args, "embed_positions_msa", False):
            emb_dim = getattr(self.args, "embed_positions_msa_dim", self.args.hidden_size)
            self.msa_position_embedding = nn.Parameter(
                0.01 * torch.randn(1, 1024, 1, emb_dim),
                requires_grad=True,
            )
        else:
            self.register_parameter("msa_position_embedding", None)
        self.emb_layer_norm_before = ESM1bLayerNorm(self.args.hidden_size)

        self.dropout_module = nn.Dropout(self.args.hidden_dropout)
        self.layers = nn.ModuleList(
            [
                AxialTransformerLayer(
                    self.args.hidden_size,
                    self.args.intermediate_hidden_size,
                    self.args.num_attention_heads,
                    self.args.hidden_dropout,
                    self.args.attention_dropout,
                    self.args.activation_dropout,
                    getattr(self.args, "max_tokens_per_msa", self.args.max_tokens),
                )
                for _ in range(self.args.num_layers)
            ]
        )

        self.emb_layer_norm_after = ESM1bLayerNorm(self.args.hidden_size)
        self.contact_head = ContactPredictionHead(
            self.args.num_layers * self.args.num_attention_heads,
            self.prepend_bos,
            self.append_eos,
            eos_idx=self.eos_idx,
        )
        # self.lm_head = RobertaLMHead(
        #     embed_dim=self.args.hidden_size,
        #     output_dim=self.alphabet_size,
        #     weight=self.embed_tokens.weight,
        # )

    def forward(self, tokens, repr_layers=[], need_head_weights=False, return_contacts=False):
        if return_contacts:
            need_head_weights = True

        assert tokens.ndim == 3
        batch_size, num_alignments, seqlen = tokens.size()
        padding_mask = tokens.eq(self.padding_idx)  # B, R, C
        if not padding_mask.any():
            padding_mask = None
        print(f"{tokens.sum()=}")
        print(f"{tokens[0][0]=}")
        print(f"{tokens[0][-1]=}")
        print(f"{tokens.shape}=")
        print(f"{self.args.hidden_size=}")
        x = self.embed_tokens(tokens)
        print(f"word_embedding.shape, {x.shape=}")
        print(f"word_embedding.sum, {x.sum()=}")
        print(f"word_embedding.sum, {x[0].sum(dim=-1)=}")
        x += self.embed_positions(tokens.view(batch_size * num_alignments, seqlen)).view(x.size())
        pos_embed = self.embed_positions(tokens.view(batch_size * num_alignments, seqlen)).view(x.size())
        print(f"{tokens.shape=}")
        print(f"pos_embed.shape, {pos_embed.shape=}")
        print(f"pos_embed.sum, {pos_embed.sum()=}")
        # print(pos_embed[0][0][0])
        # print(pos_embed[0][0][4])
        if self.msa_position_embedding is not None:
            if x.size(1) > 1024:
                raise RuntimeError(
                    "Using model with MSA position embedding trained on maximum MSA "
                    f"depth of 1024, but received {x.size(1)} alignments."
                )
            x += self.msa_position_embedding[:, :num_alignments]

        x = self.emb_layer_norm_before(x)

        x = self.dropout_module(x)

        if padding_mask is not None:
            x = x * (1 - padding_mask.unsqueeze(-1).type_as(x))

        repr_layers = set(repr_layers)
        hidden_representations = {}
        if 0 in repr_layers:
            hidden_representations[0] = x

        if need_head_weights:
            row_attn_weights = []
            col_attn_weights = []

        # B x R x C x D -> R x C x B x D
        x = x.permute(1, 2, 0, 3)

        for layer_idx, layer in enumerate(self.layers):
            x = layer(
                x,
                self_attn_padding_mask=padding_mask,
                need_head_weights=need_head_weights,
            )
            print(f"{layer_idx} x.sum() {x[0][0].sum()}")
            if need_head_weights:
                x, col_attn, row_attn = x
                # H x C x B x R x R -> B x H x C x R x R
                col_attn_weights.append(col_attn.permute(2, 0, 1, 3, 4))
                # H x B x C x C -> B x H x C x C
                row_attn_weights.append(row_attn.permute(1, 0, 2, 3))
            if (layer_idx + 1) in repr_layers:
                hidden_representations[layer_idx + 1] = x.permute(2, 0, 1, 3)

        x = self.emb_layer_norm_after(x)
        x = x.permute(2, 0, 1, 3)  # R x C x B x D -> B x R x C x D

        # last hidden representation should have layer norm applied
        if (layer_idx + 1) in repr_layers:
            hidden_representations[layer_idx + 1] = x
        # x = self.lm_head(x)

        # fake logits, real representation (last layer)
        result = {"logits": x, "representations": hidden_representations}
        if need_head_weights:
            # col_attentions: B x L x H x C x R x R
            col_attentions = torch.stack(col_attn_weights, 1)
            # row_attentions: B x L x H x C x C
            row_attentions = torch.stack(row_attn_weights, 1)
            result["col_attentions"] = col_attentions
            result["row_attentions"] = row_attentions
            if return_contacts:
                contacts = self.contact_head(tokens, row_attentions)
                result["contacts"] = contacts

        return result

    def predict_contacts(self, tokens):
        return self(tokens, return_contacts=True)["contacts"]

    def predict_heads(self, tokens):
        return self(tokens, need_head_weights=True)

    def predict_tots(self, tokens):
        return self(tokens, return_contacts=True)

    @property
    def num_layers(self):
        return self.args.num_layers

    def max_tokens_per_msa_(self, value: int) -> None:
        """The MSA Transformer automatically batches attention computations when
        gradients are disabled to allow you to pass in larger MSAs at test time than
        you can fit in GPU memory. By default this occurs when more than 2^14 tokens
        are passed in the input MSA. You can set this value to infinity to disable
        this behavior.
        """
        for module in self.modules():
            if isinstance(module, (RowSelfAttention, ColumnSelfAttention)):
                module.max_tokens_per_msa = value
