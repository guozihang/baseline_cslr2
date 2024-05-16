"""
Python file defining the training loop of the model.
"""
from typing import List, Optional

import torch
import torch.nn as nn
import wandb
from omegaconf import DictConfig
from torch.utils.data import DataLoader
from tqdm import tqdm

from utils.gather import all_gather


def train_loop(
    model: nn.Module,
    train_loader: DataLoader,
    opt: torch.optim.Optimizer,
    sent_ret_loss_fn: Optional[torch.nn.Module],
    sign_ret_loss_fn: Optional[torch.nn.Module],
    sign_cls_loss_fn: Optional[torch.nn.Module],
    epoch: int,
    cfg: DictConfig,
) -> List[float]:
    """
    Training Loop.

    Args:
        model (nn.Module): model to train
        train_loader (DataLoader): training DataLoader
        opt (torch.optim.Optimizer): optimizer
        sent_ret_loss_fn (Optional[torch.nn.Module]): sentence retrieval loss function
        sign_ret_loss_fn (Optional[torch.nn.Module]): sign retrieval loss function
        sign_cls_loss_fn (Optional[torch.nn.Module]): sign classification loss function
        epoch (int): current epoch
        cfg (DictConfig): config file

    Returns:
        List[float]: train loss, sent_ret, sign_ret, sign_cls
    """
    model.train()
    total_train_loss = 0
    total_sent_ret, total_sign_ret, total_sign_cls = 0, 0, 0
    device = torch.device(
        f"cuda:{cfg.local_rank}" if torch.cuda.is_available() else "cpu")
    pbar = tqdm(iter(train_loader)) if cfg.do_print else iter(train_loader)
    for batch_idx, batch in enumerate(pbar):
        model.zero_grad()
        opt.zero_grad()
        # unpack the batch
        subs, feats, target_indices, target_labels, target_word_embds, _, _, _, _ = batch
        feats = feats.to(device)
        word_embds = torch.cat(target_word_embds).to(device) \
            if target_word_embds[0] is not None else None
        # forward pass on the model
        cls_tokens, video_tokens, sentence_embds, word_embds, output_tensor = model(
            video_features=feats,
            subtitles=subs,
            word_embds=word_embds,
        )

        # computation of the different terms of the loss
        # computation of SentRet
        if sent_ret_loss_fn is not None:
            if cfg.distributed:
                cls_tokens = torch.cat(all_gather(cls_tokens), dim=0)
                sentence_embds = torch.cat(all_gather(sentence_embds), dim=0)
            sent_ret = sent_ret_loss_fn(cls_tokens, sentence_embds)
        else:
            sent_ret = torch.tensor([0]).to(device)

        # get the indices on where to compute the SignCls and SignRet losses
        if sign_cls_loss_fn is not None or sign_ret_loss_fn is not None:
            target_indices_batch_idx = torch.repeat_interleave(
                input=torch.arange(len(subs)),
                repeats=torch.tensor(
                    [len(target_index) for target_index in target_indices]
                ),
            )
            target_indices = torch.cat(target_indices)

        # computation of SignCls loss
        if sign_cls_loss_fn is not None:
            target_labels = torch.cat(target_labels).to(device, torch.long)
            predicted_logits = output_tensor[
                target_indices_batch_idx, target_indices
            ]
            if cfg.loss.sign_cls._target_ == "torch.nn.BCEWithLogitsLoss":
                one_hot_target = torch.zeros_like(predicted_logits).to(device)
                one_hot_target[torch.arange(
                    len(target_labels)), target_labels] = 1
                temp_target_labels, target_labels = target_labels, one_hot_target
            sign_cls = sign_cls_loss_fn(predicted_logits, target_labels)
            if cfg.loss.sign_cls._target_ == "torch.nn.BCEWithLogitsLoss":
                target_labels = temp_target_labels
        else:
            sign_cls = torch.tensor([0]).to(device)

        # computation of SignRet loss
        if sign_ret_loss_fn is not None:
            if sign_cls_loss_fn is None:
                target_labels = torch.cat(target_labels).to(device, torch.long)
            sign_ret = sign_ret_loss_fn(
                video_tokens[target_indices_batch_idx, target_indices],
                word_embds,
                labels=target_labels,
            )
        else:
            sign_ret = torch.tensor([0]).to(device)

        # weighted sum of losses
        total_loss = cfg.loss.lda_sent_ret * sent_ret + \
            cfg.loss.lda_sign_ret * sign_ret + \
            cfg.loss.lda_sign_cls * sign_cls

        total_loss.backward()
        opt.step()
        torch.cuda.synchronize()

        # prepare for printing / logging
        current_loss = total_loss.detach().item()
        total_train_loss += current_loss
        current_sent_ret = sent_ret.detach().item()
        total_sent_ret += current_sent_ret
        current_sign_ret = sign_ret.detach().item()
        total_sign_ret += current_sign_ret
        current_sign_cls = sign_cls.detach().item()
        total_sign_cls += current_sign_cls
        if cfg.do_print:
            pbar.set_postfix(
                {
                    "Loss": f"{current_loss:.2f}",
                    "SentRet": f"{current_sent_ret:.2f}",
                    "SignRet": f"{current_sign_ret:.2f}",
                    "SignCls": f"{current_sign_cls:.2f}",
                }
            )
        if cfg.do_print:
            wandb.log(
                {
                    "train_loss_iter": current_loss,
                    "train_sent_ret_iter": current_sent_ret,
                    "train_sign_ret_iter": current_sign_ret,
                    "train_sign_cls_iter": current_sign_cls,
                    "train_iter": epoch * len(train_loader) + batch_idx,
                }
            )
    return total_train_loss / len(train_loader), total_sent_ret / len(train_loader), \
        total_sign_ret / len(train_loader), total_sign_cls / len(train_loader)
