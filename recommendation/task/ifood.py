import ast
import functools
import json
import math
import os
from itertools import starmap
import multiprocessing as mp
from multiprocessing.pool import Pool
from typing import Dict, Tuple, List
import numpy as np

import luigi
import numpy as np
import pandas as pd
import torch
from tqdm import tqdm
import timeit

from recommendation.rank_metrics import precision_at_k, average_precision, ndcg_at_k
from recommendation.task.data_preparation.ifood import PrepareIfoodIndexedOrdersTestData, \
    ListAccountMerchantTuplesForIfoodIndexedOrdersTestData, IndexAccountsAndMerchantsOfInteractionsDataset
from recommendation.task.evaluation import BaseEvaluationTask
from recommendation.utils import chunks
from recommendation.task.data_preparation.base import WINDOW_FILTER_DF


def _generate_relevance_list(account_idx: int, ordered_merchant_idx: int, merchant_idx_list: List[int],
                             scores_per_tuple: Dict[Tuple[int, int], float]) -> List[int]:
    scores = list(map(lambda merchant_idx: scores_per_tuple[(account_idx, merchant_idx)], merchant_idx_list))
    sorted_merchant_idx_list = [merchant_idx for _, merchant_idx in
                                sorted(zip(scores, merchant_idx_list), reverse=True)]
    return [1 if merchant_idx == ordered_merchant_idx else 0 for merchant_idx in sorted_merchant_idx_list]


def _generate_random_relevance_list(ordered_merchant_idx: int, merchant_idx_list: List[int]) -> List[int]:
    np.random.shuffle(merchant_idx_list)
    return [1 if merchant_idx == ordered_merchant_idx else 0 for merchant_idx in merchant_idx_list]


def _generate_relevance_list_from_merchant_scores(ordered_merchant_idx: int, merchant_idx_list: List[int],
                                                  scores_per_merchant: Dict[int, float]) -> List[int]:
    scores = list(map(lambda merchant_idx: scores_per_merchant[merchant_idx], merchant_idx_list))
    sorted_merchant_idx_list = [merchant_idx for _, merchant_idx in
                                sorted(zip(scores, merchant_idx_list), reverse=True)]
    return [1 if merchant_idx == ordered_merchant_idx else 0 for merchant_idx in sorted_merchant_idx_list]

class GenerateRelevanceListsForIfoodModel(BaseEvaluationTask):
    batch_size: int = luigi.IntParameter(default=100000)

    # num_processes: int = luigi.IntParameter(default=os.cpu_count())

    def requires(self):
        return PrepareIfoodIndexedOrdersTestData(window_filter=self.window_filter), \
            ListAccountMerchantTuplesForIfoodIndexedOrdersTestData(window_filter=self.window_filter)

    def output(self):
        return luigi.LocalTarget(
            os.path.join("output", "evaluation", self.__class__.__name__, "results",
                         self.model_task_id, "orders_with_relevance_lists.csv"))

    def _evaluate_account_merchant_tuples(self) -> Dict[Tuple[int, int], float]:
        print("Reading tuples files...")
        tuples_df = pd.read_parquet(self.input()[1].path)

        assert self.model_training.project_config.input_columns[0].name == "account_idx"
        assert self.model_training.project_config.input_columns[1].name == "merchant_idx"

        print("Loading trained model...")
        module = self.model_training.get_trained_module()
        scores: List[float] = []
        print("Running the model for every account and merchant tuple...")
        for indices in tqdm(chunks(range(len(tuples_df)), self.batch_size),
                            total=math.ceil(len(tuples_df) / self.batch_size)):
            rows: pd.DataFrame = tuples_df.iloc[indices]
            batch_scores: torch.Tensor = module(
                torch.tensor(rows["account_idx"].values, dtype=torch.int64).to(self.model_training.torch_device),
                torch.tensor(rows["merchant_idx"].values, dtype=torch.int64).to(self.model_training.torch_device))
            scores.extend(batch_scores.detach().cpu().numpy())

        print("Creating the dictionary of scores...")
        return {(account_idx, merchant_idx): score for account_idx, merchant_idx, score
                in tqdm(zip(tuples_df["account_idx"], tuples_df["merchant_idx"], scores), total=len(scores))}

    def run(self):
        os.makedirs(os.path.split(self.output().path)[0], exist_ok=True)

        scores_per_tuple = self._evaluate_account_merchant_tuples()

        print("Reading the orders DataFrame...")
        orders_df: pd.DataFrame = pd.read_parquet(self.input()[0].path)

        print("Filtering orders where the ordered merchant isn't in the list...")
        orders_df = orders_df[orders_df.apply(lambda row: row["merchant_idx"] in row["merchant_idx_list"], axis=1)]

        print("Generating the relevance lists...")
        orders_df["relevance_list"] = list(tqdm(
            starmap(functools.partial(_generate_relevance_list, scores_per_tuple=scores_per_tuple),
                    zip(orders_df["account_idx"], orders_df["merchant_idx"], orders_df["merchant_idx_list"])),
            total=len(orders_df)))

        # with mp.Manager() as manager:
        #     shared_scores_per_tuple: Dict[Tuple[int, int], float] = manager.dict(scores_per_tuple)
        #     with manager.Pool(self.num_processes) as p:
        #         orders_df["relevance_list"] = list(tqdm(
        #             starmap(functools.partial(_generate_relevance_list, scores_per_tuple=shared_scores_per_tuple),
        #                     zip(orders_df["account_idx"], orders_df["merchant_idx"], orders_df["merchant_idx_list"])),
        #             total=len(orders_df)))

        print("Saving the output file...")
        orders_df[["order_id", "relevance_list"]].to_csv(self.output().path, index=False)


class GenerateReconstructedInteractionMatrix(GenerateRelevanceListsForIfoodModel):

    def _evaluate_account_merchant_tuples(self) -> Dict[Tuple[int, int], float]:
        print("Reading tuples files...")
        tuples_df = pd.read_parquet(self.input()[1].path)

        print("Grouping by account index...")
        groups = tuples_df.groupby('account_idx')['merchant_idx'].apply(list)

        int_matrix = self.model_training.train_dataset

        assert self.model_training.project_config.input_columns[0].name == "buys_per_merchant"

        print("Loading trained model...")
        module = self.model_training.get_trained_module()

        rel_dict: Dict[Tuple[int, int], float] = {}

        print("Running the model for every account and merchant tuple...")
        for indices in tqdm(chunks(list(groups.keys()), self.batch_size),
                            total=math.ceil(len(groups.keys()) / self.batch_size)):
            batch_tensors: torch.Tensor = module(
                torch.tensor(int_matrix[indices][0]).to(self.model_training.torch_device))
            merchants_lists = list(map(lambda _: groups[_], indices))
            merchants_tensor_ids = tuple(np.concatenate([ i * np.ones(len(merchants_lists[i]), dtype=np.int32) for i in range(len(merchants_lists))]).ravel())
            account_ids = tuple(np.concatenate([ indices[i] * np.ones(len(merchants_lists[i]), dtype=np.int32) for i in range(len(merchants_lists))]).ravel())
            merchants_lists = tuple([y for x in merchants_lists for y in x])
            batch_np_tensors = batch_tensors.detach().cpu().numpy()
            batch_scores = batch_np_tensors[[merchants_tensor_ids, merchants_lists]]

            rel_dict.update( {(account_idx, merchant_idx): score for account_idx, merchant_idx, score
                in zip(account_ids, merchants_lists, batch_scores)} )
            
        return rel_dict

class EvaluateIfoodModel(BaseEvaluationTask):
    num_processes: int = luigi.IntParameter(default=os.cpu_count())

    def requires(self):
        return GenerateRelevanceListsForIfoodModel(model_module=self.model_module, model_cls=self.model_cls,
                                                   model_task_id=self.model_task_id,window_filter=self.window_filter)

    def output(self):
        model_path = os.path.join("output", "evaluation", self.__class__.__name__, "results",
                                  self.model_task_id)
        return luigi.LocalTarget(os.path.join(model_path, "orders_with_metrics.csv")), \
               luigi.LocalTarget(os.path.join(model_path, "metrics.json")),

    def run(self):
        os.makedirs(os.path.split(self.output()[0].path)[0], exist_ok=True)

        df: pd.DataFrame = pd.read_csv(self.input().path)

        with Pool(self.num_processes) as p:
            df["relevance_list"] = list(tqdm(p.map(ast.literal_eval, df["relevance_list"]), total=len(df)))

            # df["precision_at_5"] = list(
            #     tqdm(p.map(functools.partial(precision_at_k, k=5), df["relevance_list"]), total=len(df)))
            # df["precision_at_10"] = list(
            #     tqdm(p.map(functools.partial(precision_at_k, k=10), df["relevance_list"]), total=len(df)))
            # df["precision_at_15"] = list(
            #     tqdm(p.map(functools.partial(precision_at_k, k=15), df["relevance_list"]), total=len(df)))

            df["average_precision"] = list(
                tqdm(p.map(average_precision, df["relevance_list"]), total=len(df)))

            df["ndcg_at_5"] = list(
                tqdm(p.map(functools.partial(ndcg_at_k, k=5), df["relevance_list"]), total=len(df)))
            df["ndcg_at_10"] = list(
                tqdm(p.map(functools.partial(ndcg_at_k, k=10), df["relevance_list"]), total=len(df)))
            df["ndcg_at_15"] = list(
                tqdm(p.map(functools.partial(ndcg_at_k, k=15), df["relevance_list"]), total=len(df)))
            df["ndcg_at_20"] = list(
                tqdm(p.map(functools.partial(ndcg_at_k, k=20), df["relevance_list"]), total=len(df)))
            df["ndcg_at_50"] = list(
                tqdm(p.map(functools.partial(ndcg_at_k, k=50), df["relevance_list"]), total=len(df)))

        df = df.drop(columns=["relevance_list"])

        metrics = {
            # "precision_at_5": df["precision_at_5"].mean(),
            # "precision_at_10": df["precision_at_10"].mean(),
            # "precision_at_15": df["precision_at_15"].mean(),
            "count": len(df),
            "average_precision": df["average_precision"].mean(),
            "ndcg_at_5": df["ndcg_at_5"].mean(),
            "ndcg_at_10": df["ndcg_at_10"].mean(),
            "ndcg_at_15": df["ndcg_at_15"].mean(),
            "ndcg_at_20": df["ndcg_at_20"].mean(),
            "ndcg_at_50": df["ndcg_at_50"].mean(),
        }

        df.to_csv(self.output()[0].path)
        with open(self.output()[1].path, "w") as metrics_file:
            json.dump(metrics, metrics_file, indent=4)


class GenerateRandomRelevanceLists(luigi.Task):
    window_filter: str = luigi.ChoiceParameter(choices=WINDOW_FILTER_DF.keys(), default="one_week")

    def requires(self):
        return PrepareIfoodIndexedOrdersTestData(window_filter=self.window_filter)

    def output(self):
        return luigi.LocalTarget(
            os.path.join("output", "evaluation", self.__class__.__name__, "results",
                         self.task_id, "orders_with_relevance_lists.csv"))

    def run(self):
        os.makedirs(os.path.split(self.output().path)[0], exist_ok=True)

        print("Reading the orders DataFrame...")
        orders_df: pd.DataFrame = pd.read_parquet(self.input().path)

        print("Filtering orders where the ordered merchant isn't in the list...")
        orders_df = orders_df[orders_df.apply(lambda row: row["merchant_idx"] in row["merchant_idx_list"], axis=1)]

        print("Generating the relevance lists...")
        orders_df["relevance_list"] = list(tqdm(
            starmap(_generate_random_relevance_list,
                    zip(orders_df["merchant_idx"], orders_df["merchant_idx_list"])),
            total=len(orders_df)))

        print("Saving the output file...")
        orders_df[["order_id", "relevance_list"]].to_csv(self.output().path, index=False)

class EvaluateIfoodCDAEModel(EvaluateIfoodModel):
    def requires(self):
        return GenerateReconstructedInteractionMatrix(model_module=self.model_module, model_cls=self.model_cls,
                                                            model_task_id=self.model_task_id, window_filter=self.window_filter)

class EvaluateRandomIfoodModel(EvaluateIfoodModel):
    model_task_id: str = luigi.Parameter(default="none")

    def requires(self):
        return GenerateRandomRelevanceLists(window_filter=self.window_filter)


class GenerateMostPopularRelevanceLists(luigi.Task):
    model_task_id: str = luigi.Parameter(default="none")
    window_filter: str = luigi.ChoiceParameter(choices=WINDOW_FILTER_DF.keys(), default="one_week")

    def requires(self):
        return IndexAccountsAndMerchantsOfInteractionsDataset(window_filter=self.window_filter), \
                PrepareIfoodIndexedOrdersTestData(window_filter=self.window_filter)

    def output(self):
        return luigi.LocalTarget(
            os.path.join("output", "evaluation", self.__class__.__name__, "results",
                         self.task_id, "orders_with_relevance_lists.csv"))

    def run(self):
        os.makedirs(os.path.split(self.output().path)[0], exist_ok=True)

        print("Reading the interactions DataFrame...")
        interactions_df: pd.DataFrame = pd.read_csv(self.input()[0].path)
        print("Generating the scores")
        scores: pd.Series = interactions_df.groupby("merchant_idx")["buys"].sum()
        scores_dict: Dict[int, float] = {merchant_idx: score for merchant_idx, score
                                         in tqdm(zip(scores.index, scores),
                                                 total=len(scores))}

        print("Reading the orders DataFrame...")
        orders_df: pd.DataFrame = pd.read_parquet(self.input()[1].path)

        print("Filtering orders where the ordered merchant isn't in the list...")
        orders_df = orders_df[orders_df.apply(lambda row: row["merchant_idx"] in row["merchant_idx_list"], axis=1)]

        print("Generating the relevance lists...")
        orders_df["relevance_list"] = list(tqdm(
            starmap(functools.partial(_generate_relevance_list_from_merchant_scores, scores_per_merchant=scores_dict),
                    zip(orders_df["merchant_idx"], orders_df["merchant_idx_list"])),
            total=len(orders_df)))

        print("Saving the output file...")
        orders_df[["order_id", "relevance_list"]].to_csv(self.output().path, index=False)


class EvaluateMostPopularIfoodModel(EvaluateIfoodModel):
    model_task_id: str = luigi.Parameter(default="none")

    def requires(self):
        return GenerateMostPopularRelevanceLists(window_filter=self.window_filter)