import torch.nn as nn
import torch
import torch.nn.functional as F
import numpy as np
import math
import torch.nn.init as init
from models import units
import copy
import os


class Embedding(torch.nn.Embedding):

    def __init__(self, num_embeddings, embedding_dim, padding_idx=None,
                 max_norm=None, norm_type=2., scale_grad_by_freq=False,
                 sparse=False, _weight=None):
        super(Embedding, self).__init__(num_embeddings, embedding_dim, padding_idx=padding_idx,
                                        max_norm=max_norm, norm_type=norm_type, scale_grad_by_freq=scale_grad_by_freq,
                                        sparse=sparse, _weight=_weight)

    def reset_parameters(self):
        init.kaiming_uniform_(self.weight, a=math.sqrt(5))
        if self.padding_idx is not None:
            with torch.no_grad():
                self.weight[self.padding_idx].fill_(0)


class ScaledDotProductAttention(nn.Module):
    """Scaled dot-product attention mechanism."""

    def __init__(self, attention_dropout=0.0):
        super(ScaledDotProductAttention, self).__init__()
        self.dropout = nn.Dropout(attention_dropout)
        self.softmax = nn.Softmax(dim=2)

    def forward(self, q, k, v, scale=None, attn_mask=None):
        attention = torch.bmm(q, k.transpose(1, 2))
        if scale:
            attention = attention * scale
        if attn_mask is not None:
            attention = attention.masked_fill_(attn_mask, -np.inf)
        attention = self.softmax(attention)
        attention = self.dropout(attention)
        context = torch.bmm(attention, v)
        return context, attention


class PositionalEncoding(nn.Module):

    def __init__(self, d_model, max_seq_len):

        super(PositionalEncoding, self).__init__()


        position_encoding = np.array([
            [pos / np.power(10000, 2.0 * (j // 2) / d_model) for j in range(d_model)]
            for pos in range(max_seq_len)])

        position_encoding[:, 0::2] = np.sin(position_encoding[:, 0::2])
        position_encoding[:, 1::2] = np.cos(position_encoding[:, 1::2])
        position_encoding = torch.from_numpy(position_encoding.astype(np.float32))

        pad_row = torch.zeros([1, d_model])
        position_encoding = torch.cat((pad_row, position_encoding))

        self.position_encoding = nn.Embedding(max_seq_len + 1, d_model)
        self.position_encoding.weight = nn.Parameter(position_encoding,
                                                     requires_grad=False)

    def forward(self, input_len):


        max_len = torch.max(input_len)
        tensor = torch.cuda.LongTensor if input_len.is_cuda else torch.LongTensor

        pos = np.zeros([len(input_len), max_len])
        for ind, length in enumerate(input_len):
            for pos_ind in range(1, length + 1):
                pos[ind, pos_ind - 1] = pos_ind
        input_pos = tensor(pos)
        return self.position_encoding(input_pos), input_pos


class PositionalWiseFeedForward(nn.Module):
    def __init__(self, model_dim=512, ffn_dim=2048, dropout=0.0):
        super(PositionalWiseFeedForward, self).__init__()
        self.w1 = nn.Conv1d(model_dim, ffn_dim, 1)
        self.w2 = nn.Conv1d(ffn_dim, model_dim, 1)
        self.dropout = nn.Dropout(dropout)
        self.layer_norm = nn.LayerNorm(model_dim)

    def forward(self, x):
        # print("x shape: ", x.shape)
        output = x.transpose(1, 2)
        output = self.w2(F.relu(self.w1(output)))
        output = self.dropout(output.transpose(1, 2))

        # add residual and norm layer
        output = self.layer_norm(x + output)
        # print("output shape", output.shape)
        return output


class MultiHeadAttention(nn.Module):
    def __init__(self, model_dim=512, num_heads=8, dropout=0.0):
        super(MultiHeadAttention, self).__init__()
        self.dim_per_head = model_dim // num_heads
        self.num_heads = num_heads
        self.linear_k = nn.Linear(model_dim, self.dim_per_head * num_heads)
        self.linear_v = nn.Linear(model_dim, self.dim_per_head * num_heads)
        self.linear_q = nn.Linear(model_dim, self.dim_per_head * num_heads)

        self.dot_product_attention = ScaledDotProductAttention(dropout)
        self.linear_final = nn.Linear(model_dim, model_dim)
        self.dropout = nn.Dropout(dropout)
        self.layer_norm = nn.LayerNorm(model_dim)

    def forward(self, key, value, query, attn_mask=None):
        residual = query

        dim_per_head = self.dim_per_head
        num_heads = self.num_heads
        batch_size = key.size(0)

        # linear projection
        key = self.linear_k(key)
        value = self.linear_v(value)
        query = self.linear_q(query)
        # print("key shape: ", key.shape)
        # split by heads
        key = key.view(batch_size * num_heads, -1, dim_per_head)
        value = value.view(batch_size * num_heads, -1, dim_per_head)
        query = query.view(batch_size * num_heads, -1, dim_per_head)

        # print("key2 shape: ", key.shape)
        if attn_mask is not None:
            attn_mask = attn_mask.repeat(num_heads, 1, 1)
        # scaled dot product attention
        scale = (key.size(-1) // num_heads) ** -0.5
        context, attention = self.dot_product_attention(
            query, key, value, scale, attn_mask)

        # concat heads
        context = context.view(batch_size, -1, dim_per_head * num_heads)

        # final linear projection
        output = self.linear_final(context)

        # dropout
        output = self.dropout(output)

        # add residual and norm layer
        output = self.layer_norm(residual + output)

        return output, attention


class EncoderLayer(nn.Module):
    def __init__(self, model_dim=512, num_heads=8, ffn_dim=2018, dropout=0.0):
        super(EncoderLayer, self).__init__()

        self.attention = MultiHeadAttention(model_dim, num_heads, dropout)
        self.feed_forward = PositionalWiseFeedForward(model_dim, ffn_dim, dropout)

    def forward(self, inputs, attn_mask=None):
        # self attention
        context, attention = self.attention(inputs, inputs, inputs, attn_mask)

        # feed forward network
        output = self.feed_forward(context)
        # print("context shape",context.shape)
        # print("attention shape", attention.shape)
        return output, attention


def padding_mask(seq_k, seq_q):
    len_q = seq_q.size(1)
    pad_mask = seq_k.eq(0)
    pad_mask = pad_mask.unsqueeze(1).expand(-1, len_q, -1)  # shape [B, L_q, L_k]
    return pad_mask


def padding_mask_sand(seq_k, seq_q):
    len_q = seq_q.size(1)
    pad_mask = seq_k.eq(0)
    pad_mask = pad_mask.unsqueeze(1).expand(-1, len_q, -1)  # shape [B, L_q, L_k]
    return pad_mask


class Encoder(nn.Module):
    def __init__(self,
                 vocab_size,
                 max_seq_len,
                 num_layers=1,
                 model_dim=256,
                 num_heads=4,
                 ffn_dim=1024,
                 dropout=0.0):
        super(Encoder, self).__init__()

        self.encoder_layers = nn.ModuleList(
            [EncoderLayer(model_dim, num_heads, ffn_dim, dropout) for _ in
             range(num_layers)])
        self.pre_embedding = nn.Linear(vocab_size, model_dim)
        self.weight_layer = torch.nn.Linear(model_dim, 1)
        self.pos_embedding = PositionalEncoding(model_dim, max_seq_len)
        self.time_layer = torch.nn.Linear(64, 256)
        self.selection_layer = torch.nn.Linear(1, 64)
        self.relu = nn.ReLU()
        self.tanh = nn.Tanh()

    def forward(self, diagnosis_codes, mask, seq_time_step, input_len):
        diagnosis_codes = diagnosis_codes.permute(1, 0, 2)
        seq_time_step = torch.Tensor(seq_time_step).cuda().unsqueeze(2) / 180
        time_feature = 1 - self.tanh(torch.pow(self.selection_layer(seq_time_step), 2))
        time_feature = self.time_layer(time_feature)
        mask = mask.permute(1, 0, 2)
        output = self.pre_embedding(diagnosis_codes)
        output += time_feature
        output_pos, ind_pos = self.pos_embedding(input_len.unsqueeze(1))
        output += output_pos
        self_attention_mask = padding_mask(ind_pos, ind_pos)

        attentions = []
        outputs = []
        for encoder in self.encoder_layers:
            output, attention = encoder(output, self_attention_mask)
            attentions.append(attention)
            outputs.append(output)
        weight = torch.softmax(self.weight_layer(outputs[-1]), dim=1)
        weight = weight * mask - 255 * (1 - mask)
        output = outputs[-1].permute(1, 0, 2)
        weight = weight.permute(1, 0, 2)
        return output, weight


class EncoderNew(nn.Module):
    def __init__(self,
                 vocab_size,
                 max_seq_len,
                 num_layers=1,
                 model_dim=256,
                 num_heads=4,
                 ffn_dim=1024,
                 dropout=0.0,
                 dropout_emb=0.1):
        super(EncoderNew, self).__init__()
        self.encoder_layers = nn.ModuleList(
            [EncoderLayer(model_dim, num_heads, ffn_dim, dropout) for _ in
             range(num_layers)])
        self.pre_embedding = Embedding(vocab_size + 1, model_dim)
        self.bias_embedding = torch.nn.Parameter(torch.Tensor(model_dim))
        bound = 1 / math.sqrt(vocab_size)
        init.uniform_(self.bias_embedding, -bound, bound)

        self.vocab_size = vocab_size
        self.target_embedding = Embedding(1, model_dim)
        #test
        self.embedding = Embedding(vocab_size+1, model_dim, padding_idx=-1)
        self.model_dim = model_dim
        self.max_seq_len = max_seq_len
        self.emb_dropout = nn.Dropout(dropout_emb)
        self.code_selection = nn.Sequential(nn.Linear(2*model_dim, model_dim), nn.Tanh(), nn.Linear(model_dim, 2))



        self.pos_embedding = PositionalEncoding(model_dim, max_seq_len)
        self.time_layer = torch.nn.Linear(64, 256)
        self.selection_layer = torch.nn.Linear(1, 64)
        self.relu = nn.ReLU()
        self.tanh = nn.Tanh()    


    def forward(self, diagnosis_codes, mask, mask_code, seq_time_step, input_len):
        seq_time_step = torch.Tensor(seq_time_step).cuda().unsqueeze(2) / 180
        time_feature = 1 - self.tanh(torch.pow(self.selection_layer(seq_time_step), 2))
        time_feature = self.time_layer(time_feature)

        # print("diagnosis_codes, mask, mask_code, seq_time_step, input_len", diagnosis_codes.shape, mask.shape, mask_code.shape, seq_time_step.shape, input_len)
        x = self.pre_embedding(diagnosis_codes)* mask_code
        x = self.emb_dropout(x)
        cat_features = torch.cat((self.embedding(torch.arange(0, self.vocab_size + 1).to(x.device)), 
                                  self.target_embedding(x.new_zeros(self.vocab_size + 1).long())), dim=-1)
        # print("self.pre_embedding(diagnosis_codes) shape:", self.embedding(diagnosis_codes.to(x.device)).shape)
        
        p = self.code_selection(cat_features)
        p = F.gumbel_softmax(torch.log_softmax(p, -1), hard=True)
        p_mask = p[:, 0].scatter_(dim=0, index=torch.LongTensor([self.vocab_size]).to(x.device), src=torch.zeros(1).to(x.device))[diagnosis_codes]
        # select_e = p_mask.unsqueeze(-1) * x
        # with open("log.txt", "a") as f:
        #     # f.write(str(self.embedding(torch.arange(0, self.vocab_size + 1).to(x.device)).shape))
        #     # f.write("-----------------11--------\n")
        #     f.write (str(self.embedding(torch.arange(0, self.vocab_size + 1).to(x.device))))
        #     f.write("-----------------22--------\n")
        #     # f.write(str(self.target_embedding(x.new_zeros(self.vocab_size + 1).long()).shape))
        #     # f.write("-----------------33--------\n")
        #     f.write(str(self.embedding(x.new_zeros(self.vocab_size + 1).long())))
        #     f.write("-----------------44--------\n")
        output = (x * p_mask.unsqueeze(-1)).sum(dim=2) + self.bias_embedding # Code Selection Mechanism
        output += time_feature
        output_pos, ind_pos = self.pos_embedding(input_len.unsqueeze(1))
        output += output_pos
        
        # print("diagnosis_codes:", diagnosis_codes.shape)
        # print("mask_code shape:", mask_code.shape)
        # print("self.pre_embedding(diagnosis_codes) shape:", (self.pre_embedding(diagnosis_codes) * mask_code).shape)
        # print("output shape: ", output.shape)
        self_attention_mask = padding_mask(ind_pos, ind_pos)

        attentions = []
        outputs = []
        for encoder in self.encoder_layers:
            output, attention = encoder(output, self_attention_mask)
            attentions.append(attention)
            outputs.append(output)
        return output

class EncoderEval(nn.Module):
    def __init__(self,
                 vocab_size,
                 max_seq_len,
                 num_layers=1,
                 model_dim=256,
                 num_heads=4,
                 ffn_dim=1024,
                 dropout=0.0):
        super(EncoderEval, self).__init__()

        self.encoder_layers = nn.ModuleList(
            [EncoderLayer(model_dim, num_heads, ffn_dim, dropout) for _ in
             range(num_layers)])
        self.pre_embedding = Embedding(vocab_size, model_dim)
        self.bias_embedding = torch.nn.Parameter(torch.Tensor(model_dim))
        bound = 1 / math.sqrt(vocab_size)
        init.uniform_(self.bias_embedding, -bound, bound)

        # self.weight_layer = torch.nn.Linear(model_dim, 1)
        self.pos_embedding = PositionalEncoding(model_dim, max_seq_len)
        self.time_layer = torch.nn.Linear(64, 256)
        self.selection_layer = torch.nn.Linear(1, 64)
        self.relu = nn.ReLU()
        self.tanh = nn.Tanh()

    def forward(self, diagnosis_codes, mask, mask_code, seq_time_step, input_len):
        seq_time_step = torch.Tensor(seq_time_step).cuda().unsqueeze(2) / 180
        time_feature = 1 - self.tanh(torch.pow(self.selection_layer(seq_time_step), 2))
        time_feature = self.time_layer(time_feature)
        output = (self.pre_embedding(diagnosis_codes) * mask_code).sum(dim=2) + self.bias_embedding

        
        cat_features = torch.cat((self.pre_embedding(torch.arange(0, self.vocab_size + 1).to(output.device)), self.target_embedding(output.new_zeros(self.vocab_size + 1).long())), dim=-1)
        p = self.code_selection(cat_features)
        p = F.gumbel_softmax(torch.log_softmax(p, -1), hard=True)
        p_mask = p[:, 0].scatter_(dim=-1, index=torch.LongTensor([self.vocab_size]).to(output.device), src=torch.zeros(1).to(output.device))[diagnosis_codes]
        select_mask_code = p_mask.unsqueeze(-1) * mask_code

        output = (self.pre_embedding(diagnosis_codes) * select_mask_code).sum(dim=2) + self.bias_embedding
        output += time_feature
        output_pos, ind_pos = self.pos_embedding(input_len.unsqueeze(1))
        output += output_pos
        self_attention_mask = padding_mask(ind_pos, ind_pos)

        attentions = []
        outputs = []
        for encoder in self.encoder_layers:
            output, attention = encoder(output, self_attention_mask)
            attentions.append(attention)
            outputs.append(output)
        # weight = torch.softmax(self.weight_layer(outputs[-1]), dim=1)
        # weight = weight * mask - 255 * (1 - mask)
        return output, attention

class EncoderPure(nn.Module):
    def __init__(self,
                 vocab_size,
                 max_seq_len,
                 num_layers=1,
                 model_dim=256,
                 num_heads=4,
                 ffn_dim=1024,
                 dropout=0.0):
        super(EncoderPure, self).__init__()

        self.encoder_layers = nn.ModuleList(
            [EncoderLayer(model_dim, num_heads, ffn_dim, dropout) for _ in
             range(num_layers)])
        self.pre_embedding = Embedding(vocab_size, model_dim)
        self.bias_embedding = torch.nn.Parameter(torch.Tensor(model_dim))
        bound = 1 / math.sqrt(vocab_size)
        init.uniform_(self.bias_embedding, -bound, bound)

        # self.weight_layer = torch.nn.Linear(model_dim, 1)
        self.pos_embedding = PositionalEncoding(model_dim, max_seq_len)
        # self.time_layer = torch.nn.Linear(64, model_dim)
        self.selection_layer = torch.nn.Linear(1, 64)
        self.relu = nn.ReLU()
        self.tanh = nn.Tanh()

    def forward(self, diagnosis_codes, mask, mask_code, seq_time_step, input_len):
        # seq_time_step = torch.Tensor(seq_time_step).cuda().unsqueeze(2)/180
        # time_feature = 1 - self.tanh(torch.pow(self.selection_layer(seq_time_step), 2))
        # time_feature = self.time_layer(time_feature)
        output = (self.pre_embedding(diagnosis_codes) * mask_code).sum(dim=2) + self.bias_embedding
        # output += time_feature
        output_pos, ind_pos = self.pos_embedding(input_len.unsqueeze(1))
        output += output_pos
        self_attention_mask = padding_mask(ind_pos, ind_pos)

        attentions = []
        outputs = []
        for encoder in self.encoder_layers:
            output, attention = encoder(output, self_attention_mask)
            attentions.append(attention)
            outputs.append(output)
        # weight = torch.softmax(self.weight_layer(outputs[-1]), dim=1)
        # weight = weight * mask - 255 * (1 - mask)
        return output



def adjust_input(batch_diagnosis_codes, batch_time_step, max_len, n_diagnosis_codes):
    batch_time_step = copy.deepcopy(batch_time_step)
    batch_diagnosis_codes = copy.deepcopy(batch_diagnosis_codes)
    for ind in range(len(batch_diagnosis_codes)):
        if len(batch_diagnosis_codes[ind]) > max_len:
            batch_diagnosis_codes[ind] = batch_diagnosis_codes[ind][-(max_len):]
            batch_time_step[ind] = batch_time_step[ind][-(max_len):]
        batch_time_step[ind].append(0)
        batch_diagnosis_codes[ind].append([n_diagnosis_codes - 1])
    return batch_diagnosis_codes, batch_time_step

class TimeEncoder(nn.Module):
    def __init__(self, batch_size):
        super(TimeEncoder, self).__init__()
        self.batch_size = batch_size
        self.selection_layer = torch.nn.Linear(1, 64)
        self.relu = nn.ReLU()
        self.tanh = nn.Tanh()
        self.weight_layer = torch.nn.Linear(64, 64)

    def forward(self, seq_time_step, final_queries, options, mask):
        if options['use_gpu']:
            seq_time_step = torch.Tensor(seq_time_step).unsqueeze(2).cuda() / 180
        else:
            seq_time_step = torch.Tensor(seq_time_step).unsqueeze(2) / 180
        selection_feature = 1 - self.tanh(torch.pow(self.selection_layer(seq_time_step), 2))
        selection_feature = self.relu(self.weight_layer(selection_feature))
        selection_feature = torch.sum(selection_feature * final_queries, 2, keepdim=True) / 8
        selection_feature = selection_feature.masked_fill_(mask, -np.inf)
        # time_weights = self.weight_layer(selection_feature)
        return torch.softmax(selection_feature, 1)


class TransformerTime(nn.Module):
    def __init__(self, n_diagnosis_codes, batch_size, options):
        super(TransformerTime, self).__init__()
        # self.prior_encoder = PriorEncoder(batch_size, options)
        self.time_encoder = TimeEncoder(batch_size)
        self.feature_encoder = EncoderNew(options['n_diagnosis_codes'] + 1, 51, num_layers=options['layer'])
        self.self_layer = torch.nn.Linear(256, 1)
        self.classify_layer = torch.nn.Linear(256, 2)
        self.quiry_layer = torch.nn.Linear(256, 64)
        self.quiry_weight_layer = torch.nn.Linear(256, 2)
        self.relu = nn.ReLU(inplace=True)
        # dropout layer
        dropout_rate = options['dropout_rate']
        self.dropout = nn.Dropout(dropout_rate)

    def get_self_attention(self, features, query, mask):
        attention = torch.softmax(self.self_layer(features).masked_fill(mask, -np.inf), dim=1)
        # attention = torch.sum(key * query, 2, keepdim=True) / 8
        return attention

    def forward(self, seq_dignosis_codes, seq_time_step, batch_labels, options, maxlen):
        # seq_dignosis_codes: [batch_size, length, bag_len]
        # seq_time_step: [batch_size, length] the day times to the final visit
        # batch_labels: [batch_size] 0 negative 1 positive
        seq_time_step = np.array(list(units.pad_time(seq_time_step, options)))
        lengths = torch.from_numpy(np.array([len(seq) for seq in seq_dignosis_codes])).cuda()
        diagnosis_codes, labels, mask, mask_final, mask_code = units.pad_matrix_new(seq_dignosis_codes,
                                                                                        batch_labels, options)
        if options['use_gpu']:
            diagnosis_codes = torch.LongTensor(diagnosis_codes).cuda()
            mask_mult = torch.BoolTensor(1-mask).unsqueeze(2).cuda()
            mask_final = torch.Tensor(mask_final).unsqueeze(2).cuda()
            mask_code = torch.Tensor(mask_code).unsqueeze(3).cuda()
        else:
            diagnosis_codes = torch.LongTensor(diagnosis_codes)
            mask_mult = torch.BoolTensor(1-mask).unsqueeze(2)
            mask_final = torch.Tensor(mask_final).unsqueeze(2)
            mask_code = torch.Tensor(mask_code).unsqueeze(3)
        features = self.feature_encoder(diagnosis_codes, mask_mult, mask_code, seq_time_step, lengths)
        final_statues = features * mask_final
        final_statues = final_statues.sum(1, keepdim=True)
        quiryes = self.relu(self.quiry_layer(final_statues))

        # prior_weight = self.prior_encoder(seq_dignosis_codes, maxlen, quiryes, options, mask_mult)
        self_weight = self.get_self_attention(features, quiryes, mask_mult)
        time_weight = self.time_encoder(seq_time_step, quiryes, options, mask_mult)
        attention_weight = torch.softmax(self.quiry_weight_layer(final_statues), 2)

        total_weight = torch.cat((time_weight, self_weight), 2)
        total_weight = torch.sum(total_weight * attention_weight, 2, keepdim=True)
        total_weight = total_weight / (torch.sum(total_weight, 1, keepdim=True) + 1e-5)
        weighted_features = features * total_weight
        averaged_features = torch.sum(weighted_features, 1)
        averaged_features = self.dropout(averaged_features)
        predictions = self.classify_layer(averaged_features)
        labels = torch.LongTensor(labels)
        if options['use_gpu']:
            labels = labels.cuda()
        return predictions, labels, self_weight


class TransformerTimeAtt(nn.Module):
    def __init__(self, n_diagnosis_codes, batch_size, options):
        super(TransformerTimeAtt, self).__init__()
        # self.prior_encoder = PriorEncoder(batch_size, options)
        self.time_encoder = TimeEncoder(batch_size)
        self.feature_encoder = EncoderPure(options['n_diagnosis_codes'] + 1, 51, num_layers=options['layer'])
        self.self_layer = torch.nn.Linear(256, 1)
        self.classify_layer = torch.nn.Linear(256, 2)
        self.quiry_layer = torch.nn.Linear(256, 64)
        self.quiry_weight_layer = torch.nn.Linear(256, 2)
        self.relu = nn.ReLU(inplace=True)
        # dropout layer
        dropout_rate = options['dropout_rate']
        self.dropout = nn.Dropout(dropout_rate)

    def get_self_attention(self, features, query, mask):
        attention = torch.softmax(self.self_layer(features).masked_fill_(mask, -np.inf), dim=1)
        # attention = torch.sum(key * query, 2, keepdim=True) / 8
        return attention

    def forward(self, seq_dignosis_codes, seq_time_step, batch_labels, options, maxlen):
        seq_time_step = np.array(list(units.pad_time(seq_time_step, options)))
        lengths = torch.from_numpy(np.array([len(seq) for seq in seq_dignosis_codes])).cuda()
        diagnosis_codes, labels, mask, mask_final, mask_code = units.pad_matrix_new(seq_dignosis_codes,
                                                                                        batch_labels, options)
        if options['use_gpu']:
            diagnosis_codes = torch.LongTensor(diagnosis_codes).cuda()
            mask_mult = torch.BoolTensor(1 - mask).unsqueeze(2).cuda()
            mask_final = torch.Tensor(mask_final).unsqueeze(2).cuda()
            mask_code = torch.Tensor(mask_code).unsqueeze(3).cuda()
        else:
            diagnosis_codes = torch.LongTensor(diagnosis_codes)
            mask_mult = torch.BoolTensor(1 - mask).unsqueeze(2)
            mask_final = torch.Tensor(mask_final).unsqueeze(2)
            mask_code = torch.Tensor(mask_code).unsqueeze(3)
        features = self.feature_encoder(diagnosis_codes, mask_mult, mask_code, seq_time_step, lengths)
        final_statues = features * mask_final
        final_statues = final_statues.sum(1, keepdim=True)
        quiryes = self.relu(self.quiry_layer(final_statues))

        # prior_weight = self.prior_encoder(seq_dignosis_codes, maxlen, quiryes, options, mask_mult)
        self_weight = self.get_self_attention(features, quiryes, mask_mult)
        time_weight = self.time_encoder(seq_time_step, quiryes, options, mask_mult)
        attention_weight = torch.softmax(self.quiry_weight_layer(final_statues), 2)

        total_weight = torch.cat((time_weight, self_weight), 2)
        total_weight = torch.sum(total_weight * attention_weight, 2, keepdim=True)
        total_weight = total_weight / (torch.sum(total_weight, 1, keepdim=True) + 1e-5)
        weighted_features = features * total_weight
        averaged_features = torch.sum(weighted_features, 1)
        averaged_features = self.dropout(averaged_features)
        predictions = self.classify_layer(averaged_features)
        labels = torch.LongTensor(labels)
        if options['use_gpu']:
            labels = labels.cuda()
        return predictions, labels, self_weight


class TransformerTimeEmb(nn.Module):
    def __init__(self, n_diagnosis_codes, batch_size, options):
        super(TransformerTimeEmb, self).__init__()
        # self.prior_encoder = PriorEncoder(batch_size, options)
        self.time_encoder = TimeEncoder(batch_size)
        self.feature_encoder = EncoderNew(options['n_diagnosis_codes'] + 1, 51, num_layers=options['layer'])
        self.self_layer = torch.nn.Linear(256, 1)
        self.classify_layer = torch.nn.Linear(256, 2)
        self.quiry_layer = torch.nn.Linear(256, 64)
        self.quiry_weight_layer = torch.nn.Linear(256, 2)
        self.relu = nn.ReLU(inplace=True)
        # dropout layer
        dropout_rate = options['dropout_rate']
        self.dropout = nn.Dropout(dropout_rate)

    def get_self_attention(self, features, query, mask):
        attention = torch.softmax(self.self_layer(features).masked_fill_(mask, -np.inf), dim=1)
        # attention = torch.sum(key * query, 2, keepdim=True) / 8
        return attention

    def forward(self, seq_dignosis_codes, seq_time_step, batch_labels, options, maxlen):
        seq_time_step = np.array(list(units.pad_time(seq_time_step, options)))
        lengths = torch.from_numpy(np.array([len(seq) for seq in seq_dignosis_codes])).cuda()
        diagnosis_codes, labels, mask, mask_final, mask_code = units.pad_matrix_new(seq_dignosis_codes,
                                                                                        batch_labels, options)
        if options['use_gpu']:
            diagnosis_codes = torch.LongTensor(diagnosis_codes).cuda()
            mask_mult = torch.BoolTensor(1 - mask).unsqueeze(2).cuda()
            mask_final = torch.Tensor(mask_final).unsqueeze(2).cuda()
            mask_code = torch.Tensor(mask_code).unsqueeze(3).cuda()
        else:
            diagnosis_codes = torch.LongTensor(diagnosis_codes)
            mask_mult = torch.BoolTensor(1 - mask).unsqueeze(2)
            mask_final = torch.Tensor(mask_final).unsqueeze(2)
            mask_code = torch.Tensor(mask_code).unsqueeze(3)
        features = self.feature_encoder(diagnosis_codes, mask_mult, mask_code, seq_time_step, lengths)
        final_statues = features * mask_final
        final_statues = final_statues.sum(1, keepdim=True)
        quiryes = self.relu(self.quiry_layer(final_statues))

        # prior_weight = self.prior_encoder(seq_dignosis_codes, maxlen, quiryes, options, mask_mult)
        self_weight = self.get_self_attention(features, quiryes, mask_mult)
        # time_weight = self.time_encoder(seq_time_step, quiryes, options, mask_mult)
        # attention_weight = torch.softmax(self.quiry_weight_layer(final_statues), 2)

        # total_weight = torch.cat((time_weight, self_weight), 2)
        total_weight = self_weight
        total_weight = total_weight / (torch.sum(total_weight, 1, keepdim=True) + 1e-5)
        weighted_features = features * total_weight
        averaged_features = torch.sum(weighted_features, 1)
        averaged_features = self.dropout(averaged_features)
        predictions = self.classify_layer(averaged_features)
        labels = torch.LongTensor(labels)
        if options['use_gpu']:
            labels = labels.cuda()
        return predictions, labels, self_weight


class TransformerSelf(nn.Module):
    def __init__(self, n_diagnosis_codes, batch_size, options):
        super(TransformerSelf, self).__init__()
        # self.prior_encoder = PriorEncoder(batch_size, options)
        # self.time_encoder = TimeEncoder(batch_size)
        self.feature_encoder = EncoderPure(options['n_diagnosis_codes'] + 1, 51, num_layers=options['layer'])
        self.self_layer = torch.nn.Linear(256, 1)
        self.classify_layer = torch.nn.Linear(256, 2)
        self.quiry_layer = torch.nn.Linear(256, 64)
        self.quiry_weight_layer = torch.nn.Linear(256, 2)
        self.relu = nn.ReLU(inplace=True)
        # dropout layer
        dropout_rate = options['dropout_rate']
        self.dropout = nn.Dropout(dropout_rate)

    def get_self_attention(self, features, query, mask):
        attention = torch.softmax(self.self_layer(features).masked_fill_(mask, -np.inf), dim=1)
        # attention = torch.sum(key * query, 2, keepdim=True) / 8
        return attention

    def forward(self, seq_dignosis_codes, seq_time_step, batch_labels, options, maxlen):
        seq_time_step = np.array(list(units.pad_time(seq_time_step, options)))
        lengths = torch.from_numpy(np.array([len(seq) for seq in seq_dignosis_codes])).cuda()
        diagnosis_codes, labels, mask, mask_final, mask_code = units.pad_matrix_new(seq_dignosis_codes,
                                                                                        batch_labels, options)
        if options['use_gpu']:
            diagnosis_codes = torch.LongTensor(diagnosis_codes).cuda()
            mask_mult = torch.BoolTensor(1 - mask).unsqueeze(2).cuda()
            mask_final = torch.Tensor(mask_final).unsqueeze(2).cuda()
            mask_code = torch.Tensor(mask_code).unsqueeze(3).cuda()
        else:
            diagnosis_codes = torch.LongTensor(diagnosis_codes)
            mask_mult = torch.BoolTensor(1 - mask).unsqueeze(2)
            mask_final = torch.Tensor(mask_final).unsqueeze(2)
            mask_code = torch.Tensor(mask_code).unsqueeze(3)
        features = self.feature_encoder(diagnosis_codes, mask_mult, mask_code, seq_time_step, lengths)
        final_statues = features * mask_final
        final_statues = final_statues.sum(1, keepdim=True)
        quiryes = self.relu(self.quiry_layer(final_statues))

        # prior_weight = self.prior_encoder(seq_dignosis_codes, maxlen, quiryes, options, mask_mult)
        self_weight = self.get_self_attention(features, quiryes, mask_mult)
        # time_weight = self.time_encoder(seq_time_step, quiryes, options, mask_mult)
        # attention_weight = torch.softmax(self.quiry_weight_layer(final_statues), 2)

        # total_weight = torch.cat((time_weight, self_weight), 2)
        total_weight = self_weight
        total_weight = total_weight / (torch.sum(total_weight, 1, keepdim=True) + 1e-5)
        weighted_features = features * total_weight
        averaged_features = torch.sum(weighted_features, 1)
        averaged_features = self.dropout(averaged_features)
        predictions = self.classify_layer(averaged_features)
        labels = torch.LongTensor(labels)
        if options['use_gpu']:
            labels = labels.cuda()
        return predictions, labels, self_weight


class TransformerFinal(nn.Module):
    def __init__(self, n_diagnosis_codes, batch_size, options):
        super(TransformerFinal, self).__init__()
        # self.prior_encoder = PriorEncoder(batch_size, options)
        # self.time_encoder = TimeEncoder(batch_size)
        self.feature_encoder = EncoderPure(options['n_diagnosis_codes'] + 1, 51, num_layers=options['layer'])
        self.self_layer = torch.nn.Linear(256, 1)
        self.classify_layer = torch.nn.Linear(256, 2)
        self.quiry_layer = torch.nn.Linear(256, 64)
        self.quiry_weight_layer = torch.nn.Linear(256, 2)
        self.relu = nn.ReLU(inplace=True)
        # dropout layer
        dropout_rate = options['dropout_rate']
        self.dropout = nn.Dropout(dropout_rate)


    def forward(self, seq_dignosis_codes, seq_time_step, batch_labels, options, maxlen):
        seq_time_step = np.array(list(units.pad_time(seq_time_step, options)))
        lengths = torch.from_numpy(np.array([len(seq) for seq in seq_dignosis_codes])).cuda()
        diagnosis_codes, labels, mask, mask_final, mask_code = units.pad_matrix_new(seq_dignosis_codes,
                                                                                        batch_labels, options)
        if options['use_gpu']:
            diagnosis_codes = torch.LongTensor(diagnosis_codes).cuda()
            mask_mult = torch.BoolTensor(1 - mask).unsqueeze(2).cuda()
            mask_final = torch.Tensor(mask_final).unsqueeze(2).cuda()
            mask_code = torch.Tensor(mask_code).unsqueeze(3).cuda()
        else:
            diagnosis_codes = torch.LongTensor(diagnosis_codes)
            mask_mult = torch.BoolTensor(1 - mask).unsqueeze(2)
            mask_final = torch.Tensor(mask_final).unsqueeze(2)
            mask_code = torch.Tensor(mask_code).unsqueeze(3)
        features = self.feature_encoder(diagnosis_codes, mask_mult, mask_code, seq_time_step, lengths)
        final_statues = features * mask_final
        final_statues = final_statues.sum(1)

        predictions = self.classify_layer(final_statues)
        labels = torch.LongTensor(labels)
        if options['use_gpu']:
            labels = labels.cuda()
        return predictions, labels, None


