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
"""Crash recovery and ownership checks for demo resources."""
import contextlib
import fcntl
import json
import os
import uuid


def save(path, state):
  """Replace the journal atomically, including a flush before cloud mutations."""
  path.parent.mkdir(parents=True, exist_ok=True)
  temporary = path.with_suffix('.tmp')
  with temporary.open('w', encoding='utf-8') as stream:
    json.dump(state, stream, indent=2)
    stream.flush()
    os.fsync(stream.fileno())
  temporary.replace(path)


@contextlib.contextmanager
def journal(path, project, region):
  """Serialize local callers and reject manifests from other environments."""
  path.parent.mkdir(parents=True, exist_ok=True)
  with path.with_suffix('.lock').open('w', encoding='utf-8') as lock:
    try:
      fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
      raise RuntimeError(
          'another workflow command holds this manifest') from error
    state = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
        'owner': 'anomaly-detection-workflow',
        'run_id': uuid.uuid4().hex[:16],
        'project': project,
        'region': region,
        'completed': []
    }
    if (state.get('owner') != 'anomaly-detection-workflow' or
        (state.get('project'), state.get('region')) != (project, region)):
      raise ValueError('manifest ownership/project/region mismatch')
    save(path, state)
    yield state


def owned(resource, state):
  """Require the exact environment and workflow label before reusing/deleting."""
  parts = resource.resource_name.split('/')
  if (len(parts) != 6 or parts[0] != 'projects' or parts[2] != 'locations' or
      parts[3] != state['region'] or
      parts[1] not in (state['project'], state.get('resource_project'))):
    raise ValueError('resource outside manifest project/region')
  if resource.labels.get('workflow_run') != state['run_id']:
    raise ValueError('resource ownership label mismatch')


def resolve(path, state, key, search, create):
  """Reconcile an interrupted create; never retry an ambiguous mutation."""
  resources = list(search())
  if len(resources) > 1:
    raise ValueError(f'multiple matching {key} resources; reconcile manually')
  if resources:
    resource = resources[0]
    # Search is explicitly scoped to project/region and the unique label.
    if resource.labels.get('workflow_run') != state['run_id']:
      raise ValueError('resource ownership label mismatch')
  else:
    pending = state.setdefault('pending', [])
    if key in pending:
      raise RuntimeError(
          f'{key} creation outcome is unknown. Wait and retry reconciliation; '
          'if no operation/resource exists, remove only this pending marker '
          'from the manifest before retrying. Do not delete the manifest.')
    pending.append(key)
    save(path, state)
    resource = create()
  # First creation/search is scoped by the initialized SDK project. Remember
  # its canonical project number, since Vertex may return it instead of the ID.
  parts = resource.resource_name.split('/')
  if len(parts) == 6 and parts[1].isdigit():
    state.setdefault('resource_project', parts[1])
  owned(resource, state)
  state[key] = resource.resource_name
  state['pending'] = [item for item in state.get('pending', []) if item != key]
  save(path, state)
  return resource


def grant_predictor(endpoint_name, project, region, service_account, role):
  """Add worker predict access to one endpoint, preserving policy and etag."""
  from google.cloud import aiplatform_v1  # pylint: disable=import-outside-toplevel
  if not role.startswith(f'projects/{project}/roles/'):
    raise ValueError('prediction role must belong to this project')
  client = aiplatform_v1.EndpointServiceClient(
      client_options={'api_endpoint': f'{region}-aiplatform.googleapis.com'})
  policy = client.get_iam_policy(
      request={'resource': endpoint_name}, timeout=30)
  member = 'serviceAccount:' + service_account
  for binding in policy.bindings:
    if binding.role == role and not binding.HasField('condition'):
      if member not in binding.members:
        binding.members.append(member)
      break
  else:
    policy.bindings.add(role=role, members=[member])
  client.set_iam_policy(
      request={
          'resource': endpoint_name,
          'policy': policy
      }, timeout=30)
