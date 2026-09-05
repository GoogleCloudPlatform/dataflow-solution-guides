#  Copyright 2026 Google LLC
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
"""Python 3.14 deployment and bounded demonstration commands.

Creation intent is persisted before API calls. Re-running a stage reconciles
owned resources; cleanup uses only recorded resource names, never display-name search.
"""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
from google.api_core.exceptions import NotFound
from google.cloud import aiplatform, aiplatform_v1, storage
from google.cloud.aiplatform_v1.types import JobState
from .demo import publish, required as env, seed

from .prediction import predict_batch
from .lifecycle import grant_predictor, journal, owned, resolve, save


def execute(args, state, path):
  project, region = env('PROJECT'), env('REGION')
  aiplatform.init(project=project, location=region)
  run_id = state['run_id']
  if args.command == 'train':
    bucket = env('BUCKET').removeprefix('gs://')
    if state.get('artifact_bucket', bucket) != bucket:
      raise ValueError('BUCKET differs from the saved training configuration')
    state['artifact_bucket'] = bucket
    if 'training_job' not in state:
      image = env('TRAINING_CONTAINER_URI')
      if '@sha256:' not in image:
        raise ValueError(
            'TRAINING_CONTAINER_URI must use a resolved @sha256 digest')
      resolve(
          path, state, 'training_job', lambda: aiplatform.CustomJob.list(
              filter=f'labels.workflow_run="{run_id}"'),
          lambda: submit_training(state, image))
    job = aiplatform.CustomJob.get(state['training_job'])
    owned(job, state)
    job.wait_for_completion()
    output_uri = job.gca_resource.job_spec.base_output_directory.output_uri_prefix.rstrip(
        '/')
    if output_uri != f'gs://{bucket}/anomaly-training/{run_id}':
      raise ValueError('training output differs from the owned artifact prefix')
    state['artifact_uri'] = output_uri + '/model'
    state['training_image'] = job.gca_resource.job_spec.worker_pool_specs[
        0].container_spec.image_uri
    state['completed'] = sorted(set(state['completed'] + ['train']))
  elif args.command == 'validate':
    validate_artifact(state, path)
  elif args.command == 'deploy':
    if 'validate' not in state['completed']:
      raise ValueError(
          'run train then validate against the serving image first')
    if 'model' not in state:
      resolve(
          path, state, 'model', lambda: aiplatform.Model.list(
              filter=f'labels.workflow_run="{run_id}"'),
          lambda: aiplatform.Model.upload(
              display_name='anomaly-' + state['run_id'],
              artifact_uri=state['artifact_uri'],
              serving_container_image_uri=state['serving_image'],
              serving_container_predict_route='/predict',
              serving_container_health_route='/health',
              serving_container_ports=[8080],
              labels={'workflow_run': state['run_id']}))
    if 'endpoint' not in state:
      resolve(
          path, state, 'endpoint', lambda: aiplatform.Endpoint.list(
              filter=f'labels.workflow_run="{run_id}"'),
          lambda: aiplatform.Endpoint.create(
              display_name='anomaly-' + state['run_id'],
              labels={'workflow_run': state['run_id']}))
    endpoint = aiplatform.Endpoint(state['endpoint'])
    owned(endpoint, state)
    owned(aiplatform.Model(state['model']), state)
    deployed = endpoint.list_models()
    if any(item.model != state['model'] for item in deployed):
      raise ValueError('endpoint contains an unowned model')
    if not deployed:
      if state.get('deploy_pending'):
        raise RuntimeError(
            'deployment outcome unknown; wait and retry; inspect Vertex '
            'operations before clearing deploy_pending')
      state['deploy_pending'] = True
      save(path, state)
      endpoint.deploy(
          aiplatform.Model(state['model']),
          machine_type=os.environ.get('ENDPOINT_MACHINE_TYPE', 'n1-standard-2'),
          min_replica_count=1,
          max_replica_count=1)
    state.pop('deploy_pending', None)
    grant_predictor(state['endpoint'], project, region, env('SERVICE_ACCOUNT'),
                    env('ENDPOINT_PREDICTOR_ROLE'))
    check_predictions(endpoint)
    endpoint_file = Path(args.endpoint_env)
    endpoint_file.parent.mkdir(parents=True, exist_ok=True)
    endpoint_file.write_text(
        'export MODEL_ENDPOINT=' +
        shlex.quote(state['endpoint'].split('/')[-1]) + '\n' +
        f'export MODEL_LOCATION={shlex.quote(region)}\n',
        encoding='utf-8')
    state['endpoint_env'] = str(endpoint_file)
    state['endpoint_env_content'] = endpoint_file.read_text(encoding='utf-8')
    state['completed'] = sorted(set(state['completed'] + ['deploy']))
  elif args.command == 'verify':
    endpoint = aiplatform.Endpoint(
        os.environ.get('MODEL_ENDPOINT') or state['endpoint'],
        location=os.environ.get('MODEL_LOCATION', region))
    result = check_predictions(endpoint)
    print(json.dumps(result))
  elif args.command == 'seed':
    seed(project)
  elif args.command in ('publish', 'smoke'):
    publish(project, args.count, args.timeout, smoke=args.command == 'smoke')
  elif args.command == 'cleanup':
    reconcile_cleanup(path, state)
    for key, constructor in [('endpoint', aiplatform.Endpoint),
                             ('model', aiplatform.Model),
                             ('training_job', aiplatform.CustomJob.get)]:
      if key not in state:
        continue
      try:
        resource = constructor(state[key])
        owned(resource, state)
        if key == 'training_job':
          if resource.state not in (JobState.JOB_STATE_SUCCEEDED,
                                    JobState.JOB_STATE_FAILED,
                                    JobState.JOB_STATE_CANCELLED,
                                    JobState.JOB_STATE_EXPIRED):
            resource.cancel()
            try:
              resource.wait_for_completion()
            except RuntimeError:
              if resource.state != JobState.JOB_STATE_CANCELLED:
                raise
        if key == 'endpoint':
          if state.get('deploy_pending') and not resource.list_models():
            raise RuntimeError('resolve the pending deployment before cleanup')
          if any(item.model != state.get('model')
                 for item in resource.list_models()):
            raise ValueError('endpoint has an unowned deployment')
          resource.undeploy_all()
        resource.delete()
      except NotFound:
        pass
      del state[key]
      save(path, state)
    cleanup_artifacts(state)
    state['completed'] = []
    state.pop('deploy_pending', None)
    endpoint_file = Path(args.endpoint_env)
    if state.get('endpoint_env') == str(
        endpoint_file) and endpoint_file.exists():
      if endpoint_file.read_text(
          encoding='utf-8') == state.get('endpoint_env_content'):
        endpoint_file.unlink()
  save(path, state)


def run(args):
  """Hold the manifest lock for the complete command, including remote waits."""
  path = Path(args.manifest)
  with journal(path, env('PROJECT'), env('REGION')) as state:
    execute(args, state, path)


def submit_training(state, image):
  """Submit a CPU job using only the dedicated artifact-writing identity."""
  state['artifact_bucket'] = env('BUCKET').removeprefix('gs://')
  artifact_bucket = state['artifact_bucket']
  run_id = state['run_id']
  job = aiplatform.CustomJob(
      display_name='anomaly-' + state['run_id'],
      worker_pool_specs=[{
          'machine_spec': {
              'machine_type':
                  os.environ.get('TRAINING_MACHINE_TYPE', 'n1-standard-4')
          },
          'replica_count': 1,
          'container_spec': {
              'image_uri': image
          }
      }],
      staging_bucket=f'gs://{artifact_bucket}',
      base_output_dir=f'gs://{artifact_bucket}/anomaly-training/{run_id}',
      labels={'workflow_run': state['run_id']})
  job.submit(service_account=env('TRAINING_SERVICE_ACCOUNT'))
  state['training_image'] = image
  return job


def check_predictions(endpoint):
  """Verify the batched scalar-label contract used by the pipeline."""
  client = aiplatform_v1.PredictionServiceClient(client_options={
      'api_endpoint': f'{endpoint.location}-aiplatform.googleapis.com'
  })
  return predict_batch(client, endpoint.resource_name,
                       [[1., 1., 1.], [10., 1500., 40.]])


def validate_artifact(state, path):
  """Download this run's trusted artifact and test it in the custom serving environment."""
  if 'train' not in state['completed']:
    raise ValueError('complete training first')
  directory = path.parent / 'model'
  directory.mkdir(exist_ok=True)
  bucket, _, prefix = state['artifact_uri'][5:].partition('/')
  client = storage.Client(project=state['project'])
  for name in ('model.joblib', 'metadata.json'):
    client.bucket(bucket).blob(prefix.rstrip('/') + '/' +
                               name).download_to_filename(directory / name)
  serving_image = env('SERVING_CONTAINER_URI')
  if '@sha256:' not in serving_image:
    raise ValueError('SERVING_CONTAINER_URI must use a resolved @sha256 digest')
  script = Path(
      __file__).resolve().parents[1] / 'scripts/verify_serving_container.sh'
  subprocess.run(['bash', str(script), str(directory.resolve())], check=True)
  state['compatibility'] = json.loads(
      (directory / 'compatibility.json').read_text(encoding='utf-8'))
  state['serving_image'] = serving_image
  state['completed'] = sorted(set(state['completed'] + ['validate']))


def reconcile_cleanup(path, state):
  """Recover any unjournaled creates before attempting deletion."""
  constructors = {
      'training_job': aiplatform.CustomJob,
      'model': aiplatform.Model,
      'endpoint': aiplatform.Endpoint
  }
  run_id = state['run_id']
  for key in list(state.get('pending', [])):
    resolve(
        path,
        state,
        key,
        lambda key=key: constructors[key].list(
            filter=f'labels.workflow_run="{run_id}"'),
        lambda: None)  # resolve refuses to create when a pending marker exists.


def cleanup_artifacts(state):
  """Delete only this run's unique artifact prefix, retaining the bucket."""
  bucket = state.get('artifact_bucket')
  if not bucket:
    return
  client = storage.Client(project=state['project'])
  run_id = state['run_id']
  prefix = f'anomaly-training/{run_id}/'
  for blob in client.list_blobs(bucket, prefix=prefix):
    blob.delete(if_generation_match=blob.generation)


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument(
      'command',
      choices=[
          'train', 'validate', 'deploy', 'verify', 'seed', 'publish', 'smoke',
          'cleanup'
      ])
  parser.add_argument('--manifest', default='.deployment/manifest.json')
  parser.add_argument(
      '--endpoint-env', default='scripts/03_endpoint_environment.sh')
  parser.add_argument('--count', type=int, default=20)
  parser.add_argument('--timeout', type=int, default=600)
  args = parser.parse_args()
  if not 1 <= args.count <= 10000 or not 1 <= args.timeout <= 3600:
    parser.error('count must be 1..10000 and timeout 1..3600 seconds')
  run(args)


if __name__ == '__main__':
  main()
