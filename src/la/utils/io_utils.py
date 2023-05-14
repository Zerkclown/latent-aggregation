from collections import namedtuple
from pathlib import Path
from la.utils.utils import MyDatasetDict, convert_to_rgb
from datasets import load_dataset, DatasetDict, load_from_disk, Dataset, concatenate_datasets, Value, Array3D

from nn_core.common import PROJECT_ROOT


def load_data(cfg):
    DatasetParams = namedtuple("DatasetParams", ["name", "fine_grained", "train_split", "test_split", "hf_key"])
    dataset_params: DatasetParams = DatasetParams(
        cfg.dataset.ref,
        None,
        cfg.dataset.train_split,
        cfg.dataset.test_split,
        (cfg.dataset.ref,),
    )
    DATASET_KEY = "_".join(
        map(
            str,
            [v for k, v in dataset_params._asdict().items() if k != "hf_key" and v is not None],
        )
    )
    DATASET_DIR: Path = PROJECT_ROOT / "data" / "encoded_data" / DATASET_KEY
    if not DATASET_DIR.exists() or not cfg.use_cached:
        train_dataset = load_dataset(
            dataset_params.name,
            split=dataset_params.train_split,
            use_auth_token=True,
        )
        test_dataset = load_dataset(dataset_params.name, split=dataset_params.test_split)
        dataset: DatasetDict = MyDatasetDict(train=train_dataset, test=test_dataset)
    else:
        dataset: Dataset = load_from_disk(dataset_path=str(DATASET_DIR))

    return dataset


def save_dataset_to_disk(dataset, output_path):
    if not isinstance(output_path, Path):
        output_path = Path(output_path)

    if not output_path.exists():
        output_path.mkdir(parents=True)

    dataset.save_to_disk(output_path)


def preprocess_dataset(dataset, cfg):
    dataset = dataset.map(
        lambda x: {cfg.label_key: x[cfg.dataset.label_key]},
        remove_columns=[cfg.dataset.label_key],
        desc="Standardizing label key",
    )
    dataset = dataset.map(
        lambda x: {cfg.image_key: x[cfg.dataset.image_key]},
        batched=True,
        remove_columns=[cfg.dataset.image_key],
        desc="Standardizing image key",
    )

    # in case some images are not RGB, convert them to RGB
    dataset = dataset.map(lambda x: {cfg.image_key: convert_to_rgb(x[cfg.image_key])}, desc="Converting to RGB")
    dataset.set_format(type="numpy", columns=[cfg.image_key, cfg.label_key])

    shape = dataset["train"][0]["img"].shape

    dataset = dataset.cast_column("img", Array3D(dtype="uint8", shape=shape, id=None))

    return dataset


def add_ids_to_dataset(dataset):
    N = len(dataset["train"])
    M = len(dataset["test"])
    indices = {"train": list(range(N)), "test": list(range(N, N + M))}

    for mode in ["train", "test"]:
        dataset[mode] = dataset[mode].map(lambda row, ind: {"id": indices[mode][ind]}, with_indices=True)

    return dataset
