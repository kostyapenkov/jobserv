from flask import Blueprint, request

from jobserv.internal_requests import internal_api
from jobserv.jsend import jsendify
from jobserv.models import ProjectTrigger, TriggerTypes

blueprint = Blueprint(
    'api_project_triggers', __name__, url_prefix='/project-triggers')


@blueprint.route('/', methods=('GET',))
@internal_api
def project_trigger_list():
    t = request.args.get('type')
    if t:
        t = TriggerTypes[t].value
        query = ProjectTrigger.query.filter(ProjectTrigger.type == t)
    else:
        query = ProjectTrigger.query.all()
    return jsendify([x.as_json() for x in query])