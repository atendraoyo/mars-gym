from typing import Tuple, List, Union, Optional, Dict

import numpy as np
import pandas as pd
from torch.utils.data import Dataset

from recommendation.task.meta_config import ProjectConfig, IOType, Column
from recommendation.utils import parallel_literal_eval


def literal_eval_array_columns(data_frame: pd.DataFrame, columns: List[Column]):
    for column in columns:
        if column.type in (IOType.FLOAT_ARRAY, IOType.INT_ARRAY, IOType.INDEX_ARRAY) and column.name in data_frame:
            data_frame[column.name] = parallel_literal_eval(data_frame[column.name])


def preprocess_interactions_data_frame(data_frame: pd.DataFrame, project_config: ProjectConfig):
    if len(data_frame) == 0:
        return data_frame
    data_frame[project_config.user_column.name] = data_frame[project_config.user_column.name].astype(int)
    data_frame[project_config.item_column.name] = data_frame[project_config.item_column.name].astype(int)
    literal_eval_array_columns(
        data_frame, [project_config.user_column, project_config.item_column, project_config.output_column]
                    + [input_column for input_column in project_config.other_input_columns])
    if project_config.available_arms_column_name and isinstance(
            data_frame.iloc[0][project_config.available_arms_column_name], str):
        data_frame[project_config.available_arms_column_name] = parallel_literal_eval(
            data_frame[project_config.available_arms_column_name])
    return data_frame


def preprocess_metadata_data_frame(metadata_data_frame: pd.DataFrame,
                                   project_config: ProjectConfig) -> Dict[str, np.ndarray]:
    metadata_data_frame = metadata_data_frame.set_index(project_config.item_column.name, drop=False).sort_index()

    if not (np.arange(0, len(metadata_data_frame.index)) == metadata_data_frame.index).all():
        raise ValueError("The item index is not contiguous")

    embeddings_for_metadata: Dict[str, np.ndarray] = {}
    for metadata_column in project_config.metadata_columns:
        emb = metadata_data_frame[metadata_column.name].values.tolist()
        embeddings_for_metadata[metadata_column.name] = np.array(emb, dtype=metadata_column.type.dtype)

    return embeddings_for_metadata


class InteractionsDataset(Dataset):
    def __init__(self, data_frame: pd.DataFrame,
                 embeddings_for_metadata: Optional[Dict[str, np.ndarray]],
                 project_config: ProjectConfig) -> None:
        self._project_config = project_config
        self._input_columns: List[Column] = project_config.input_columns
        if project_config.item_is_input:
            self._item_input_index = self._input_columns.index(project_config.item_column)

        input_column_names = [input_column.name for input_column in self._input_columns]
        auxiliar_output_column_names = [auxiliar_output_column.name
                                        for auxiliar_output_column in project_config.auxiliar_output_columns]
        self._data_frame = data_frame[
            set(input_column_names + [project_config.output_column.name] + auxiliar_output_column_names)
                .intersection(data_frame.columns)]
        self._embeddings_for_metadata = embeddings_for_metadata

    def __len__(self) -> int:
        return self._data_frame.shape[0]

    def _convert_dtype(self, value: np.ndarray, type: IOType) -> np.ndarray:
        if type == IOType.INDEX:
            return value.astype(np.int64)
        if type == IOType.NUMBER:
            return value.astype(np.float64)            
        if type in (IOType.INT_ARRAY, IOType.INDEX_ARRAY):
            return np.array([np.array(v, dtype=np.int64) for v in value])
        if type == IOType.FLOAT_ARRAY:
            return np.array([np.array(v, dtype=np.float64) for v in value])
        return value

    def __getitem__(self, indices: Union[int, List[int]]) -> Tuple[Tuple[np.ndarray, ...],
                                                                   Union[np.ndarray, Tuple[np.ndarray, ...]]]:
        if isinstance(indices, int):
            indices = [indices]
        rows: pd.Series = self._data_frame.iloc[indices]

        inputs = tuple(self._convert_dtype(rows[column.name].values, column.type) for column in self._input_columns)
        if self._project_config.item_is_input and self._embeddings_for_metadata is not None:
            item_indices = inputs[self._item_input_index]
            inputs += tuple(self._embeddings_for_metadata[column.name][item_indices]
                            for column in self._project_config.metadata_columns)

        output = self._convert_dtype(rows[self._project_config.output_column.name].values,
                                      self._project_config.output_column.type)
        if self._project_config.auxiliar_output_columns:
            output = tuple([output]) + tuple(self._convert_dtype(rows[column.name].values, column.type)
                                             for column in self._project_config.auxiliar_output_columns)
        return inputs, output
