#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""A module for meta-learner predictability.

This module contains the class :class:`MetaLearnPredictability` for meta-learner predictability. This class predicts whether a time series is predictable or not.
The predictability of a time series is determined by whether the forecasting errors of the possible best forecasting model can be less than a user-defined threshold.
"""

import ast
import logging
from typing import Dict, List, Optional, Union, Any

import joblib
import numpy as np
import pandas as pd
from kats.consts import TimeSeriesData
from kats.tsfeatures.tsfeatures import TsFeatures
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.metrics import precision_recall_curve, precision_recall_fscore_support
from sklearn.model_selection import train_test_split
from sklearn.naive_bayes import GaussianNB
from sklearn.neighbors import KNeighborsClassifier


class MetaLearnPredictability:
    """Meta-learner framework on predictability.
    This framework uses classification algorithms to predict whether a time series is predictable or not (
    we define the time series with error metrics less than a user defined threshold as predictable).
    For training, it uses time series features as inputs and whether the best forecasting models' errors less than the user-defined threshold as labels.
    For prediction, it takes time series or time series features as inputs to predict whether the corresponding time series is predictable or not.
    This class provides preprocess, pred, pred_by_feature, save_model and load_model.

    Attributes:
        metadata: Optional; A list of dictionaries representing the meta-data of time series (e.g., the meta-data generated by GetMetaData object).
                  Each dictionary d must contain at least 3 components: 'hpt_res', 'features' and 'best_model'. d['hpt_res'] represents the best hyper-parameters for each candidate model and the corresponding errors;
                  d['features'] are time series features, and d['best_model'] is a string representing the best candidate model of the corresponding time series data.
                  metadata should not be None unless load_model is True. Default is None
        threshold: Optional; A float representing the threshold for the forecasting error. A time series whose forecasting error of the best forecasting model is higher than the threshold is considered as unpredictable. Default is 0.2.
        load_model: Optional; A boolean to specify whether or not to load a trained model. Default is False.

    Sample Usage:
        >>> mlp = MetaLearnPredictability(data)
        >>> mlp.train()
        >>> mlp.save_model()
        >>> mlp.pred(TSdata) # Predict whether a time series is predictable.
        >>> mlp2 = MetaLearnPredictability(load_model=True) # Create a new object to load the trained model
        >>> mlp2.load_model()
    """

    def __init__(
        self,
        metadata: Optional[List[Any]] = None,
        threshold: float = 0.2,
        load_model=False,
    ) -> None:
        if load_model:
            msg = "Initialize this class without meta data, and a pretrained model should be loaded using .load_model() method."
            logging.info(msg)
        else:
            if metadata is None:
                msg = "Please input meta data to initialize this class."
                logging.error(msg)
                raise ValueError(msg)
            if len(metadata) <= 30:
                msg = "Dataset is too small to train a meta learner!"
                logging.error(msg)
                raise ValueError(msg)

            if "hpt_res" not in metadata[0]:
                msg = "Missing best hyper-params, not able to train a meta learner!"
                logging.error(msg)
                raise ValueError(msg)

            if "features" not in metadata[0]:
                msg = "Missing time series features, not able to train a meta learner!"
                logging.error(msg)
                raise ValueError(msg)

            if "best_model" not in metadata[0]:
                msg = "Missing best models, not able to train a meta learner!"
                logging.error(msg)
                raise ValueError(msg)

            self.metadata = metadata
            self.threshold = threshold
            self._reorganize_data()
            self._validate_data()
            self.rescale = False
            self.clf = None
            self._clf_threshold = None

    def _reorganize_data(self) -> None:
        """Reorganize raw input data into features and labels."""

        metadata = self.metadata

        self.features = []
        self.labels = []

        for i in range(len(metadata)):
            try:
                if isinstance(metadata[i]["hpt_res"], str):
                    hpt = ast.literal_eval(metadata[i]["hpt_res"])
                else:
                    hpt = metadata[i]["hpt_res"]

                if isinstance(metadata[i]["features"], str):
                    feature = ast.literal_eval(metadata[i]["features"])
                else:
                    feature = metadata[i]["features"]

                self.features.append(feature)
                self.labels.append(hpt[metadata[i]["best_model"]][1])
            except Exception as e:
                logging.exception(e)
        self.labels = (np.array(self.labels) > self.threshold).astype(int)
        self.features = pd.DataFrame(self.features)
        self.features.fillna(0, inplace=True)
        self.features_mean = np.average(self.features.values, axis=0)

        self.features_std = np.std(self.features.values, axis=0)

        self.features_std[self.features_std == 0] = 1.0

        return

    def _validate_data(self) -> None:
        """Validate input data.

        We check two aspects:
            1) whether input data contain both positive and negative instances.
            2) whether training data size is at least 30.
        """

        if len(np.unique(self.labels)) == 1:
            msg = "Only one type of time series data and cannot train a classifier!"
            logging.error(msg)
            raise ValueError(msg)
        if len(self.features) <= 30:
            msg = "Dataset is too small to train a meta learner!"
            logging.error(msg)
            raise ValueError(msg)

    def preprocess(self) -> None:
        """Rescale input time series features to zero-mean and unit-variance.

        Returns:
            None.
        """

        self.rescale = True
        features = (self.features.values - self.features_mean) / self.features_std
        self.features = pd.DataFrame(features, columns=self.features.columns)

    def train(
        self,
        method: str = "RandomForest",
        valid_size: float = 0.1,
        test_size: float = 0.1,
        recall_threshold: float = 0.7,
        n_estimators: int = 500,
        n_neighbors: int = 5,
        **kwargs,
    ) -> Dict[str, float]:
        """Train a classifier with time series features to forecast predictability.

        Args:
            method: Optional; A string representing the name of the classification algorithm. Can be 'RandomForest', 'GBDT', 'KNN' or 'NaiveBayes'. Default is 'RandomForest'.
            valid_size: Optional; A float representing the size of validation set for parameter tunning, which should be within (0, 1). Default is 0.1.
            test_size: Optional; A float representing the size of test set, which should be within [0., 1-valid_size). Default is 0.1.
            recall_threshold: Optional; A float controlling the recall score of the classifier. The recall of the trained classifier will be larger than recall_threshold. Default is 0.7.
            n_estimators: Optional; An integer representing the number of trees in random forest model. Default is 500.
            n_neighbors: Optional; An integer representing the number of neighbors in KNN model. Default is 5.

        Returns:
            A dictionary stores the classifier performance on the test set (if test_size is valid).
        """

        if method not in ["RandomForest", "GBDT", "KNN", "NaiveBayes"]:
            msg = "Only support RandomForest, GBDT, KNN, and NaiveBayes method."
            logging.error(msg)
            raise ValueError(msg)

        if valid_size <= 0.0 or valid_size >= 1.0:
            msg = "valid size should be in (0.0, 1.0)"
            logging.error(msg)
            raise ValueError(msg)

        if test_size <= 0.0 or test_size >= 1.0:
            msg = f"invalid test_size={test_size} and reset the test_size as 0."
            test_size = 0.0
            logging.warning(msg)

        n = len(self.features)
        x_train, x_valid, y_train, y_valid = train_test_split(
            self.features, self.labels, test_size=int(n * valid_size)
        )

        if test_size > 0 and test_size < (1 - valid_size):
            x_train, x_test, y_train, y_test = train_test_split(
                x_train, y_train, test_size=int(n * test_size)
            )
        elif test_size == 0:
            x_train, y_train = self.features, self.labels
            x_test, y_test = None, None
        else:
            msg = "Invalid test_size and re-set test_size as 0."
            logging.info(msg)
            x_train, y_train = self.features, self.labels
            x_test, y_test = None, None
        if method == "NaiveBayes":
            clf = GaussianNB(**kwargs)
        elif method == "GBDT":
            clf = GradientBoostingClassifier(**kwargs)
        elif method == "KNN":
            kwargs["n_neighbors"] = kwargs.get("n_neighbors", 5)
            clf = KNeighborsClassifier(**kwargs)
        else:
            kwargs["n_estimators"] = n_estimators
            kwargs["class_weight"] = kwargs.get("class_weight", "balanced_subsample")
            clf = RandomForestClassifier(**kwargs)
        clf.fit(x_train, y_train)
        pred_valid = clf.predict_proba(x_valid)[:, 1]
        p, r, threshold = precision_recall_curve(y_valid, pred_valid)
        try:
            clf_threshold = threshold[np.where(p == np.max(p[r >= recall_threshold]))][
                -1
            ]
        except Exception as e:
            msg = f"Fail to get a proper threshold for recall {recall_threshold}, use 0.5 as threshold instead. Exception message is: {e}"
            logging.warning(msg)
            clf_threshold = 0.5
        if x_test is not None:
            pred_test = clf.predict_proba(x_test)[:, 1] > clf_threshold
            precision_test, recall_test, f1_test, _ = precision_recall_fscore_support(
                y_test, pred_test, average="binary"
            )
            accuracy = np.average(pred_test == y_test)
            ans = {
                "accuracy": accuracy,
                "precision": precision_test,
                "recall": recall_test,
                "f1": f1_test,
            }
        else:
            ans = {}
        self.clf = clf
        self._clf_threshold = clf_threshold
        return ans

    def pred(self, source_ts: TimeSeriesData, ts_rescale: bool = True) -> bool:
        """Predict whether a time series is predicable or not.

        Args:
            source_ts: :class:`kats.consts.TimeSeriesData` object representing the new time series data.
            ts_scale: Optional; A boolean to specify whether or not to rescale time series data (i.e., normalizing it with its maximum vlaue) before calculating features. Default is True.

        Returns:
            A boolean representing whether the time series is predictable or not.
        """

        ts = TimeSeriesData(pd.DataFrame(source_ts.to_dataframe().copy()))
        if self.clf is None:
            msg = "No model trained yet, please train the model first."
            logging.error(msg)
            raise ValueError(msg)
        if ts_rescale:
            ts.value /= ts.value.max()
            msg = "Successful scaled! Each value of TS has been divided by the max value of TS."
            logging.info(msg)
        features = TsFeatures().transform(ts)
        x = np.array(list(features.values()))
        if np.sum(np.isnan(x)) > 0:
            msg = (
                "Features of ts contain NaN, please consider preprocessing ts. Features are: "
                f"{features}. Fill in NaNs with 0."
            )
            logging.warning(msg)
        ans = True if self.pred_by_feature([x])[0] == 1 else False
        return ans

    def pred_by_feature(
        self, source_x: Union[np.ndarray, List[np.ndarray], pd.DataFrame]
    ) -> np.ndarray:
        """Predict whether a list of time series are predicable or not given their time series features.
        Args:
            source_x: the time series features of the time series that one wants to predict, can be a np.ndarray, a list of np.ndarray or a pd.DataFrame.

        Returns:
            A np.array storing whether the corresponding time series are predictable or not.
        """

        if self.clf is None:
            msg = "No model trained yet, please train the model first."
            logging.error(msg)
            raise ValueError(msg)
        if isinstance(source_x, List):
            x = np.row_stack(source_x)
        elif isinstance(source_x, np.ndarray):
            x = source_x.copy()
        else:
            msg = f"In valid source_x type: {type(x)}."
            logging.error(msg)
            raise ValueError(msg)
        x[np.isnan(x)] = 0.0
        if self.rescale:
            x = (x - self.features_mean) / self.features_std
        pred = (self.clf.predict_proba(x)[:, 1] < self._clf_threshold).astype(int)
        return pred

    def save_model(self, file_path: str) -> None:
        """Save the trained model.

        Args:
            file_name: A string representing the path to save the trained model.

        Returns:
            None.
        """
        if self.clf is None:
            msg = "Please train the model first!"
            logging.error(msg)
            raise ValueError(msg)
        joblib.dump(self.__dict__, file_path)
        logging.info(f"Successfully save the model: {file_path}.")

    def load_model(self, file_path) -> None:
        """Load a pre-trained model.

        Args:
            file_name: A string representing the path to load the pre-trained model.

        Returns:
            None.
        """
        try:
            self.__dict__ = joblib.load(file_path)
            logging.info(f"Successfully load the model: {file_path}.")
        except Exception as e:
            msg = f"Fail to load model with Exception msg: {e}"
            logging.exception(msg)
            raise ValueError(msg)
