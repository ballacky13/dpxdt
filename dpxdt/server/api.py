#!/usr/bin/env python
# Copyright 2013 Brett Slatkin
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Web-based API for managing screenshots and incremental perceptual diffs.

Lifecycle of a release:

1. User creates a new build, which represents a single product or site that
   will be screenshotted repeatedly over time. This may happen very
   infrequenty through a web UI.

2. User creates a new release candidate for the build with a specific release
   name. The candidate is an attempt at finishing a specific release name. It
   may take many attempts, many candidates, before the release with that name
   is complete and can be marked as good.

3. User creates many runs for the candidate created in #2. Each run is
   identified by a unique name that describes what it does. For example, the
   run name could be the URL path for a page being screenshotted. The user
   associates each run with a new screenshot artifact. Runs are automatically
   associated with a corresponding run from the last good release. This makes
   it easy to compare new and old screenshots for runs with the same name.

4. User uploads a series of screenshot artifacts identified by content hash.
   Perceptual diffs between these new screenshots and the last good release
   may also be uploaded as an optimization. This may happen in parallel
   with #3.

5. The user marks the release candidate as having all of its expected runs
   present, meaning it will no longer receive new runs. This should only
   happen after all screenshot artifacts have finished uploading.

6. If a run indicates a previous screenshot, but no perceptual diff has
   been made to compare the new and old versions, a worker will do a perceptual
   diff, upload it, and associate it with the run.

7. Once all perceptual diffs for a release candidate's runs are complete,
   the results of the candidate are emailed out to the build's owner.

8. The build owner can go into a web UI, inspect the new/old perceptual diffs,
   and mark certain runs as okay even though the perceptual diff showed a
   difference. For example, a new feature will cause a perceptual diff, but
   should not be treated as a failure.

9. The user decides the release candidate looks correct and marks it as good,
   or the user thinks the candidate looks bad and goes back to #2 and begins
   creating a new candidate for that release all over again.


Notes:

- At any time, a user can manually mark any candidate or release as bad. This
  is useful to deal with bugs in the screenshotter, mistakes in approving a
  release candidate, rolling back to an earlier version, etc.

- As soon as a new release name is cut for a build, the last candidate of
  the last release is marked as good if there is no other good candidate. This
  lets the API establish a "baseline" release easily for first-time users.

- Only one release candidate may be receiving runs for a build at a time.
"""

import datetime
import hashlib
import functools
import json
import logging
import mimetypes

# Local libraries
import flask
from flask import Flask, abort, request, url_for
from flask.exceptions import HTTPException

# Local modules
from dpxdt import constants
from . import app
from . import db
import auth
import models
import work_queue
import utils


@app.route('/api/create_release', methods=['POST'])
@auth.build_api_access_required
def create_release(build):
    """Creates a new release candidate for a build."""
    release_name = request.form.get('release_name')
    utils.jsonify_assert(release_name, 'release_name required')
    url = request.form.get('url')
    utils.jsonify_assert(release_name, 'url required')

    release = models.Release(
        name=release_name,
        url=url,
        number=1,
        build_id=build.id)

    last_candidate = (
        models.Release.query
        .filter_by(build_id=build.id, name=release_name)
        .order_by(models.Release.number.desc())
        .first())
    if last_candidate:
        release.number += last_candidate.number

    db.session.add(release)
    db.session.commit()

    logging.info('Created release: build_id=%r, release_name=%r, url=%r, '
                 'release_number=%d', build.id, release.name,
                 url, release.number)

    return flask.jsonify(
        success=True,
        build_id=build.id,
        release_name=release.name,
        release_number=release.number,
        url=url)


def _check_release_done_processing(release):
    """Moves a release candidate to reviewing if all runs are done."""
    if release.status != models.Release.PROCESSING:
        logging.info('Release not in processing state yet: build_id=%r, '
                     'name=%r, number=%d', release.build_id, release.name,
                     release.number)
        return False

    query = models.Run.query.filter_by(release_id=release.id)
    for run in query:
        if run.status == models.Run.NEEDS_DIFF:
            return False
        if not run.image:
            return False

    logging.info('Release done processing, now reviewing: build_id=%r, '
                 'name=%r, number=%d', release.build_id, release.name,
                 release.number)

    release.status = models.Release.REVIEWING
    db.session.add(release)
    return True


def _get_release_params():
    """Gets the release params from the current request."""
    release_name = request.form.get('release_name')
    utils.jsonify_assert(release_name, 'release_name required')
    release_number = request.form.get('release_number', type=int)
    utils.jsonify_assert(release_number is not None, 'release_number required')
    return release_name, release_number


def _find_last_good_run(build):
    """Finds the last good release and run for a build."""
    run_name = request.form.get('run_name', type=str)
    utils.jsonify_assert(run_name, 'run_name required')

    last_good_release = (
        models.Release.query
        .filter_by(
            build_id=build.id,
            status=models.Release.GOOD)
        .order_by(models.Release.created.desc())
        .first())

    last_good_run = None

    if last_good_release:
        logging.debug('Found last good release for: build_id=%r, '
                      'release_name=%r, release_number=%d',
                      build.id, last_good_release.name,
                      last_good_release.number)
        last_good_run = (
            models.Run.query
            .filter_by(release_id=last_good_release.id, name=run_name)
            .first())
        if last_good_run:
            logging.debug('Found last good run for: build_id=%r, '
                          'release_name=%r, release_number=%d, '
                          'run_name=%r',
                          build.id, last_good_release.name,
                          last_good_release.number, last_good_run.name)

    return last_good_release, last_good_run


@app.route('/api/find_run', methods=['POST'])
@auth.build_api_access_required
def find_run(build):
    """Finds the last good run of the given name for a release."""
    last_good_release, last_good_run = _find_last_good_run(build)

    if last_good_run:
        return flask.jsonify(
            success=True,
            build_id=build.id,
            release_name=last_good_release.name,
            release_number=last_good_release.number,
            run_name=last_good_run.name,
            url=last_good_run.url,
            image=last_good_run.image,
            log=last_good_run.log,
            config=last_good_run.config)

    return utils.jsonify_error('Run not found')


def _get_or_create_run(build):
    """Gets a run for a build or creates it if it does not exist."""
    release_name, release_number = _get_release_params()
    run_name = request.form.get('run_name', type=str)
    utils.jsonify_assert(run_name, 'run_name required')

    release = (
        models.Release.query
        .filter_by(build_id=build.id, name=release_name, number=release_number)
        .first())
    utils.jsonify_assert(release, 'release does not exist')

    run = (
        models.Run.query
        .filter_by(release_id=release.id, name=run_name)
        .first())
    if not run:
        # Ignore re-reports of the same run name for this release.
        logging.info('Created run: build_id=%r, release_name=%r, '
                     'release_number=%d, run_name=%r',
                     build.id, release.name, release.number, run_name)
        run = models.Run(
            release_id=release.id,
            name=run_name,
            status=models.Run.DATA_PENDING)

    return release, run


@app.route('/api/request_run', methods=['POST'])
@auth.build_api_access_required
def request_run(build):
    """Requests a new run for a release candidate."""
    current_release, current_run = _get_or_create_run(build)
    last_good_release, last_good_run = _find_last_good_run(build)

    if last_good_run:
        current_run.ref_url = last_good_run.url
        current_run.ref_image = last_good_run.image
        current_run.ref_log = last_good_run.log
        current_run.ref_config = last_good_run.config

    current_url = request.form.get('url', type=str)
    config_data = request.form.get('config', default='{}', type=str)
    utils.jsonify_assert(current_url, 'url to capture required')
    utils.jsonify_assert(config_data, 'config document required')

    # Validate the JSON config parses.
    try:
        config_dict = json.loads(config_data)
    except Exception, e:
        return jsonify_error(e)

    # Rewrite the config JSON to include the URL specified in this request.
    # Blindly overwrite anything that was there.
    config_dict['targetUrl'] = current_url
    config_data = json.dumps(config_dict)

    config_artifact = _save_artifact(build, config_data, 'application/json')
    db.session.add(config_artifact)
    db.session.flush()

    current_run.url = current_url
    current_run.config = config_artifact.id

    work_queue.add(constants.CAPTURE_QUEUE_NAME, dict(
        build_id=build.id,
        release_name=current_release.name,
        release_number=current_release.number,
        run_name=current_run.name,
        url=current_run.url,
        config_sha1sum=current_run.config,
    ))

    db.session.add(current_run)
    db.session.commit()

    return flask.jsonify(
        success=True,
        build_id=build.id,
        release_name=current_release.name,
        release_number=current_release.number,
        run_name=current_run.name,
        url=current_run.url,
        config=current_run.config)


@app.route('/api/report_run', methods=['POST'])
@auth.build_api_access_required
def report_run(build):
    """Reports data for a run for a release candidate."""
    release, run = _get_or_create_run(build)

    current_url = request.form.get('url', type=str)
    current_image = request.form.get('image', type=str)
    current_log = request.form.get('log', type=str)
    current_config = request.form.get('config', type=str)

    ref_url = request.form.get('ref_url', type=str)
    ref_image = request.form.get('ref_image', type=str)
    ref_log = request.form.get('ref_log', type=str)
    ref_config = request.form.get('ref_config', type=str)

    diff_success = request.form.get('diff_success', type=str)
    diff_image = request.form.get('diff_image', type=str)
    diff_log = request.form.get('diff_log', type=str)

    if current_url:
        run.url = current_url
    if current_image:
        run.image = current_image
    if current_log:
        run.log = current_log
    if current_config:
        run.config = current_config
    if current_image or current_log or current_config:
        logging.info('Saved run data: build_id=%r, release_name=%r, '
                     'release_number=%d, run_name=%r, url=%r, '
                     'image=%r, log=%r, config=%r',
                     build.id, release.name, release.number, run.name,
                     run.url, run.image, run.log, run.config)

    if ref_url:
        run.ref_url = ref_url
    if ref_image:
        run.ref_image = ref_image
    if ref_log:
        run.ref_log = ref_log
    if ref_config:
        run.ref_config = ref_config
    if ref_image or ref_log or ref_config:
        logging.info('Saved reference data: build_id=%r, release_name=%r, '
                     'release_number=%d, run_name=%r, ref_url=%r, '
                     'ref_image=%r, ref_log=%r, ref_config=%r',
                     build.id, release.name, release.number, run.name,
                     run.ref_url, run.ref_image, run.ref_log, run.ref_config)

    if diff_image:
        run.diff_image = diff_image
    if diff_log:
        run.diff_log = diff_log
    if diff_image or diff_log:
        logging.info('Saved pdiff: build_id=%r, release_name=%r, '
                     'release_number=%d, run_name=%r, '
                     'diff_image=%r, diff_log=%r',
                     build.id, release.name, release.number, run.name,
                     run.diff_image, run.diff_log)

    if run.image and run.diff_image:
        run.status = models.Run.DIFF_FOUND
    elif run.image and run.ref_image and not run.diff_log:
        run.status = models.Run.NEEDS_DIFF
    elif run.image and run.ref_image and diff_success:
        run.status = models.Run.DIFF_NOT_FOUND
    elif run.image and not run.ref_image:
        run.status = models.Run.NO_DIFF_NEEDED

    # TODO: Verify the build has access to both the current_image and
    # the reference_sha1sum so they can't make a diff from a black image
    # and still see private data in the diff image.

    if run.status == models.Run.NEEDS_DIFF:
        work_queue.add(constants.PDIFF_QUEUE_NAME, dict(
            build_id=build.id,
            release_name=release.name,
            release_number=release.number,
            run_name=run.name,
            run_sha1sum=run.image,
            reference_sha1sum=run.ref_image,
        ))

    # Flush the run so querying for Runs in _check_release_done_processing
    # will be find the new run too.
    db.session.add(run)
    _check_release_done_processing(release)
    db.session.commit()

    logging.info('Updated run: build_id=%r, release_name=%r, '
                 'release_number=%d, run_name=%r, status=%r',
                 build.id, release.name, release.number, run.name, run.status)

    return flask.jsonify(success=True)


@app.route('/api/runs_done', methods=['POST'])
@auth.build_api_access_required
def runs_done(build):
    """Marks a release candidate as having all runs reported."""
    release_name, release_number = _get_release_params()

    release = (
        models.Release.query
        .filter_by(build_id=build.id, name=release_name, number=release_number)
        .first())
    utils.jsonify_assert(release, 'Release does not exist')

    release.status = models.Release.PROCESSING
    db.session.add(release)
    _check_release_done_processing(release)
    db.session.commit()

    logging.info('Runs done for release: build_id=%r, release_name=%r, '
                 'release_number=%d', build.id, release.name, release.number)

    results_url = url_for(
        'view_release',
        id=build.id,
        name=release.name,
        number=release.number,
        _external=True)

    return flask.jsonify(
        success=True,
        results_url=results_url)


def _save_artifact(build, data, content_type):
    """Saves an artifact to the DB and returns it.

    This method may be overridden in environments that have a different way of
    storing artifact files, such as on-disk or S3. Use the artifact.alternate
    field to hold the environment-specific data you need.
    """
    sha1sum = hashlib.sha1(data).hexdigest()
    artifact = models.Artifact.query.filter_by(id=sha1sum).first()

    if artifact:
      logging.debug('Upload already exists: artifact_id=%r', sha1sum)
    else:
      logging.info('Upload received: artifact_id=%r, content_type=%r',
                   sha1sum, content_type)
      artifact = models.Artifact(
          id=sha1sum,
          content_type=content_type,
          data=data)

    artifact.owners.append(build)
    return artifact


@app.route('/api/upload', methods=['POST'])
@auth.build_api_access_required
def upload(build):
    """Uploads an artifact referenced by a run."""
    utils.jsonify_assert(len(request.files) == 1,
                         'Need exactly one uploaded file')

    file_storage = request.files.values()[0]
    data = file_storage.read()
    content_type, _ = mimetypes.guess_type(file_storage.filename)

    artifact = _save_artifact(build, data, content_type)

    db.session.add(artifact)
    db.session.commit()

    return flask.jsonify(
        success=True,
        build_id=build.id,
        sha1sum=artifact.id,
        content_type=content_type)


@app.route('/api/download')
def download():
    """Downloads an artifact by it's content hash."""
    # Allow access to a resource hash for API keys with access to the given
    # build_id. Falls back to standard user login for the frontend (which
    # may require redirecting the user to the login form).
    try:
        build = auth.can_api_key_access_build('build_id')
    except HTTPException:
        build = auth.can_user_access_build('build_id')

    sha1sum = request.args.get('sha1sum', type=str)
    artifact = models.Artifact.query.get(sha1sum)
    if not artifact:
        logging.debug('Artifact sha1sum=%r does not exist', sha1sum)
        abort(404)

    build_id = request.args.get('build_id', type=int)
    if not build_id:
        logging.debug('build_id missing for artifact sha1sum=%r', sha1sum)
        abort(404)

    is_owned = artifact.owners.filter_by(id=build_id).first()
    if not is_owned:
        logging.debug('build_id=%r not owner of artifact sha1sum=%r',
                      build_id, sha1sum)
        abort(403)

    if request.if_none_match and request.if_none_match.contains(sha1sum):
        return flask.Response(status=304)

    response = flask.Response(
        artifact.data,
        mimetype=artifact.content_type)
    response.cache_control.private = True
    response.cache_control.max_age = 8640000
    response.set_etag(sha1sum)
    return response
