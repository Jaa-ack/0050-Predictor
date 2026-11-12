"""Fixed-training-history similarity expert with complete daily fallbacks.

Inspired by historical-state retrieval, not a reproduction of an index-strength
study. No transfer labels or inference-time refitting enter this model.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.neighbors import NearestNeighbors
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

RADII = {'similarity_tight': .5, 'similarity_local': 1., 'similarity_wide': 2.}


class SimilarityDirectionModel:
    def __init__(self, frame, columns, labels, radius):
        self.columns = list(columns)
        numeric = self._numeric(frame)
        self.prior = float(np.mean(labels))
        self.labels = np.asarray(labels, dtype=float)
        self.dates = pd.to_datetime(frame.date).to_numpy()
        self.radius = radius
        self.transform = make_pipeline(SimpleImputer(strategy='median', keep_empty_features=True,
                                                      add_indicator=True), StandardScaler())
        encoded = self.transform.fit_transform(numeric)
        self.dimension = encoded.shape[1]
        self.has_information = bool(numeric.notna().any().any())
        self.neighbors = NearestNeighbors(n_neighbors=min(257, len(frame)), algorithm='brute', n_jobs=1)
        self.neighbors.fit(encoded / np.sqrt(self.dimension))
        self.training_details = dict(method='shared_truncated_radius_bernoulli_mean', features=self.columns,
            fitted_rows=len(frame), fitted_dates=int(frame.date.nunique()), radius_rms=radius,
            nominal_neighbors=256, cutoff_ties='include_all_within_1e-12_tolerance',
            minimum_neighbors=20, minimum_neighbor_dates=5,
            prior_weight=10., fallback='training_class_prior', training_probability=self.prior,
            transformed_dimensions=self.dimension,
            limitation='Correlated neighbors are not independent evidence; probability is not calibrated confidence.')

    @property
    def classes_(self):
        return np.array([0, 1])

    def _numeric(self, frame):
        return frame[self.columns].apply(pd.to_numeric, errors='coerce').replace([np.inf, -np.inf], np.nan)

    def predict_proba(self, frame):
        probability = np.full(len(frame), self.prior)
        numeric = self._numeric(frame)
        if not self.has_information or not len(frame):
            return np.column_stack([1-probability, probability])
        known = numeric.notna().any(axis=1).to_numpy()
        encoded = self.transform.transform(numeric) / np.sqrt(self.dimension)
        for start in range(0, len(frame), 512):
            distances, positions = self.neighbors.kneighbors(encoded[start:start+512])
            for offset, (distance, indices) in enumerate(zip(distances, positions, strict=True)):
                if (len(indices) > 256 and distance[255] <= self.radius
                        and np.isclose(distance[255], distance[256], rtol=1e-12, atol=1e-12)):
                    tolerance = 1e-12 + abs(distance[255])*1e-12
                    tied_distance, tied_indices = self.neighbors.radius_neighbors(
                        encoded[[start+offset]], radius=distance[255]+tolerance)
                    selected = tied_indices[0][tied_distance[0] <= self.radius]
                else:
                    selected = indices[:256][distance[:256] <= self.radius]
                if (known[start+offset] and len(selected) >= 20
                        and len(np.unique(self.dates[selected])) >= 5):
                    probability[start+offset] = (self.labels[selected].sum()+10*self.prior)/(len(selected)+10)
        return np.column_stack([1-probability, probability])
