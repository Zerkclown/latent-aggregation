import json
import logging
import hydra
from os import PathLike
from pathlib import Path
from typing import Optional, Union, Dict, List
import numpy as np
from omegaconf import ListConfig

from datasets import DatasetDict, Dataset
from pytorch_lightning.callbacks import ModelCheckpoint, Callback
import torch
from tqdm import tqdm

pylogger = logging.getLogger(__name__)


class IdentityTransform:
    def __call__(self, x):
        return x


class ToFloatRange:
    def __call__(self, x):
        """
        Convert [0, 255] to [0, 1]
        :param x:
        :return:
        """
        return x.float() / 255


def encode_field(batch, src_field: str, tgt_field: str, transformation):
    """
    Create a new field with name `tgt_field` by applying `transformation` to `src_field`.
    """
    src_data = batch[src_field]
    transformed = transformation(src_data)

    return {tgt_field: transformed}


def preprocess_img(x):
    """
    (H, W, C) --> (C, H, W)
    :param x: (H, W, C)
    :return:
    """
    if x.ndim == 2:
        x = x.unsqueeze(-1)
        x = x.repeat_interleave(3, axis=2)

    if x.ndim == 4:
        x = x.permute(0, 3, 1, 2)
    else:
        x = x.permute(2, 0, 1)

    return x.float() / 255.0


class ConvertToRGB:
    def __call__(self, image):
        convert_to_rgb(image)


def convert_to_rgb(image):
    if image.mode != "RGB":
        return image.convert("RGB")

    return np.array(image)


class MyDatasetDict(DatasetDict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.data_keys = None

    def save_to_disk(
        self,
        dataset_dict_path: PathLike,
        fs="deprecated",
        max_shard_size: Optional[Union[str, int]] = None,
        num_shards: Optional[Dict[str, int]] = None,
        num_proc: Optional[int] = None,
        storage_options: Optional[dict] = None,
    ):
        self.save_metadata(Path(dataset_dict_path) / "metadata.json")
        tmp = self["metadata"]
        del self["metadata"]
        super().save_to_disk(
            dataset_dict_path,
            fs=fs,
            max_shard_size=max_shard_size,
            num_shards=num_shards,
            num_proc=num_proc,
            storage_options=storage_options,
        )
        self["metadata"] = tmp

    def set_format(
        self,
        type: Optional[str] = None,
        columns: Optional[List] = None,
        output_all_columns: bool = False,
        **format_kwargs,
    ):
        for key in self.data_keys():
            self[key].set_format(
                type=type,
                columns=columns,
                output_all_columns=output_all_columns,
                **format_kwargs,
            )

    def save_metadata(self, metadata_path: PathLike):
        with open(metadata_path, "w") as f:
            json.dump(self["metadata"], f)

    @staticmethod
    def load_from_disk(
        dataset_dict_path: PathLike,
        fs="deprecated",
        keep_in_memory: Optional[bool] = None,
        storage_options: Optional[dict] = None,
    ) -> "MyDatasetDict":
        dataset = MyDatasetDict(
            DatasetDict.load_from_disk(
                dataset_dict_path,
                fs=fs,
                keep_in_memory=keep_in_memory,
                storage_options=storage_options,
            )
        )
        dataset.data_keys = dataset.keys()

        dataset["metadata"] = json.load(open(Path(dataset_dict_path) / "metadata.json"))
        return dataset


def get_checkpoint_callback(callbacks):
    for callback in callbacks:
        if isinstance(callback, ModelCheckpoint):
            return callback


def add_tensor_column(dataset, column, tensor):
    dataset_dict = dataset.to_dict()
    dataset_dict[column] = tensor.tolist()
    dataset = Dataset.from_dict(dataset_dict)

    return dataset


def build_callbacks(cfg: ListConfig, *args: Callback) -> List[Callback]:
    """Instantiate the callbacks given their configuration.

    Args:
        cfg: a list of callbacks instantiable configuration
        *args: a list of extra callbacks already instantiated

    Returns:
        the complete list of callbacks to use
    """
    callbacks: List[Callback] = list(args)

    for callback in cfg:
        pylogger.info(f"Adding callback <{callback['_target_'].split('.')[-1]}>")
        callbacks.append(hydra.utils.instantiate(callback, _recursive_=False))

    return callbacks


def save_dict_to_file(content, path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w+") as f:
        json.dump(content, f)


def embed_task_samples(datamodule, model, task_ind, modes) -> Dict:

    datamodule.shuffle_train = False

    embeddings = {mode: None for mode in modes}

    for mode in modes:
        mode_embeddings = []

        dataloader = datamodule.dataloader(mode, only_task_specific_test=True)

        for batch in tqdm(dataloader, desc=f"Embedding {mode} samples"):
            x = batch["x"].to("cuda")
            mode_embeddings.extend(model(x)["embeds"].detach())

        embeddings[mode] = torch.stack(mode_embeddings)

    embedded_samples = {mode: None for mode in modes}

    map_params = {
        "with_indices": True,
        "batched": True,
        "batch_size": 128,
        "num_proc": 1,
        "writer_batch_size": 10,
    }

    for mode in modes:
        embedded_samples[mode] = datamodule.data[f"task_{task_ind}_{mode}"].map(
            function=lambda x, ind: {
                "embedding": embeddings[mode][ind],
            },
            desc=f"Storing embedded {mode} samples",
            **map_params,
            remove_columns=["x"],
        )

    return embedded_samples


# scatter mean from PyG
def broadcast(src: torch.Tensor, other: torch.Tensor, dim: int):
    if dim < 0:
        dim = other.dim() + dim
    if src.dim() == 1:
        for _ in range(0, dim):
            src = src.unsqueeze(0)
    for _ in range(src.dim(), other.dim()):
        src = src.unsqueeze(-1)
    src = src.expand(other.size())
    return src


def scatter_sum(
    src: torch.Tensor,
    index: torch.Tensor,
    dim: int = -1,
    out: Optional[torch.Tensor] = None,
    dim_size: Optional[int] = None,
) -> torch.Tensor:

    index = broadcast(index, src, dim)
    if out is None:
        size = list(src.size())
        if dim_size is not None:
            size[dim] = dim_size
        elif index.numel() == 0:
            size[dim] = 0
        else:
            size[dim] = int(index.max()) + 1
        out = torch.zeros(size, dtype=src.dtype, device=src.device)
        return torch.scatter_add(input=out, dim=dim, index=index, src=src)
    else:
        return torch.scatter_add(input=out, dim=dim, index=index, src=src)


def scatter_mean(
    src: torch.Tensor,
    index: torch.Tensor,
    dim: int = -1,
    out: Optional[torch.Tensor] = None,
    dim_size: Optional[int] = None,
) -> torch.Tensor:
    out = scatter_sum(src, index, dim, out, dim_size)
    dim_size = out.size(dim)

    index_dim = dim
    if index_dim < 0:
        index_dim = index_dim + src.dim()
    if index.dim() <= index_dim:
        index_dim = index.dim() - 1

    ones = torch.ones(index.size(), dtype=src.dtype, device=src.device)
    count = scatter_sum(ones, index, index_dim, None, dim_size)
    count[count < 1] = 1
    count = broadcast(count, out, dim)
    if out.is_floating_point():
        torch.true_divide(out, count)
    else:
        torch.div(out, count, rounding_mode="floor")
    return out
