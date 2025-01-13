#  Copyright 2025 Google LLC
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
"""
Pipeline of the Marketing Intelligence Dataflow Solution guide.
"""
import apache_beam as beam
from apache_beam import DoFn
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import logging


class RunInference(beam.DoFn):

  def __init__(self, model):
    self.model = model

  def process(self, element):
    df = pd.DataFrame([element])
    df['last_service_date'] = (
        pd.to_datetime(df['last_service_date']) -
        pd.to_datetime(df['last_service_date']).min()).dt.days
    prediction = self.model.predict(
        df[['max_temperature', 'max_vibration', 'last_service_date']])
    results = beam.Row(
        vehicle_id=str(element['vehicle_id']),
        max_temperature=float(element['max_temperature']),
        max_vibration=float(element['max_vibration']),
        latest_timestamp=element['latest_timestamp'],
        last_service_date=element['last_service_date'],
        maintenance_type=element['maintenance_type'],
        model=element['model'],
        needs_maintenance=prediction[0])
    # results = {**element, "needs_maintenance": prediction[0]}
    logging.info(f"Inference : {results}")
    yield results._asdict()