import hmac
import json
import traceback
import yaml

from urllib.parse import quote_plus

import requests

from flask import Blueprint, request, url_for

from jobserv.jsend import ApiError, get_or_404, jsendify
from jobserv.models import Project, ProjectTrigger, TriggerTypes
from jobserv.settings import RUN_URL_FMT
from jobserv.trigger import trigger_build

blueprint = Blueprint('api_gitlab', __name__, url_prefix='/gitlab')


@blueprint.errorhandler(ApiError)
def api_error(e):
    return e.resp


def _get_params(data):
    if data['object_kind'] == 'note':
        mr = data['merge_request']
        mr_url = data['object_attributes']['url']
    else:
        mr = data['object_attributes']
        mr_url = mr['url']

    status_url = mr['source']['web_url']
    user_repo = mr['source']['path_with_namespace']
    replace = 'api/v4/projects/' + quote_plus(user_repo)
    status_url = status_url.replace(user_repo, replace)
    status_url += '/statuses/' + mr['last_commit']['id']

    return {
        'GIT_SHA': mr['last_commit']['id'],
        'GIT_URL': mr['source']['git_http_url'],
        'GL_STATUS_URL': status_url,
        'GL_TARGET_REPO': mr['target']['git_http_url'],
        'GL_MR': mr_url,
    }


def _get_proj_def(trigger, token, params):
    if trigger.definition_repo:
        # look up defintion out-of-tree
        name = trigger.definition_file
        if not name:
            name = trigger.project.name + '.yml'
        url = trigger.definition_repo
        if url[-1] != '/':
            url += '/'
        url += 'raw/master/' + name
    else:
        # look up defintion in tree
        url = params['GIT_URL']
        assert url[-4:] == '.git'
        url = url[:-4] + '/raw/' + params['GIT_SHA'] + '/.jobserv.yml'

    headers = {
        'PRIVATE-TOKEN': token,
    }
    resp = requests.get(url, headers=headers)
    if resp.status_code == 200:
        data = yaml.load(resp.text)
        for trigger in data.get('triggers', []):
            if trigger['type'] == 'gitlab_mr':
                return trigger['name'], data
        raise ValueError('No gitlab_mr trigger types defined in ' + url)
    raise ValueError('Project definition does not exist: ' + url)


def _fail_pr(params, token, failure_url):
    headers = {
        'Content-Type': 'application/json',
        'PRIVATE-TOKEN': token,
    }
    data = {
        'context': 'JobServ',
        'description': 'unexpected failure',
        'state': 'failure',
        'target_url': failure_url,
    }
    return requests.post(params['GL_STATUS_URL'], json=data, headers=headers)


def _update_pr(build, status_url, token):
    headers = {
        'Content-Type': 'application/json',
        'PRIVATE-TOKEN': token,
    }

    for run in build.runs:
        if RUN_URL_FMT:
            url = RUN_URL_FMT.format(project=build.project.name,
                                     build=build.build_id,
                                     run=run.name)
        else:
            url = url_for('api_run.run_get', proj=build.project.name,
                          build_id=build.build_id, run=run.name,
                          _external=True)
        data = {
            'context': run.name,
            'description': 'Build %d' % build.build_id,
            'target_url': url,
            'state': 'pending',
        }
        r = requests.post(status_url, json=data, headers=headers)
        if r.status_code != 201:
            print('ERROR updating MR(%s): %d\n%s' % (
                status_url, r.status_code, r.text))


def _validate_payload(trigger):
    secrets = json.loads(trigger.secrets)
    key = secrets.get('webhook-key')
    if not key:
        raise ApiError(403, 'Trigger has no webhook-key secret defined')

    if not hmac.compare_digest(key, request.headers['X-Gitlab-Token']):
        raise ApiError(403, 'Invalid X-Gitlab-Token')


def _filter_events(event):
    events = ('Merge Request Hook', 'Note Hook')
    if event not in events:
        raise ApiError(400, 'Invalid action: ' + event)


@blueprint.route('/<proj>/', methods=('POST',))
def on_webhook(proj):
    trigger = get_or_404(ProjectTrigger.query.filter(
        ProjectTrigger.type == TriggerTypes.gitlab_mr.value
    ).join(
        Project
    ).filter(
        Project.name == proj
    ))
    _validate_payload(trigger)
    event = request.headers['X-Gitlab-Event']
    _filter_events(event)

    data = request.get_json()
    mr_actions = ('open', 'reopen', 'update')
    if event == 'Note Hook':
        if 'ci-retest' not in data['object_attributes']['note']:
            return 'Ingoring comment'
    elif data['object_attributes']['action'] not in mr_actions:
        return 'Ingoring Merge Request action'

    params = _get_params(data)

    reason = 'GitLab MR: ' + params['GL_MR']
    secrets = json.loads(trigger.secrets)
    if 'gitlabtok' not in secrets or 'gitlabuser' not in secrets:
        raise ApiError(
            400, 'Trigger secrets is missing "gitlabtok" or "gitlabuser"')
    token = secrets['gitlabtok']

    try:
        trig, proj = _get_proj_def(trigger, token, params)
        b = trigger_build(trigger.project, reason, trig, params, secrets, proj)
        _update_pr(b, params['GL_STATUS_URL'], token)
        url = url_for('api_build.build_get',
                      proj=trigger.project.name, build_id=b.build_id,
                      _external=True)
        return jsendify({'url': url}, 201)
    except ApiError as e:
        url = e.resp.headers.get('Location')
        _fail_pr(params, token, url)
        raise
    except Exception as e:
        _fail_pr(params, token, None)
        tb = traceback.format_exc()
        return 'FAILED:\n' + tb, 500