from __future__ import absolute_import, print_function

from django.core.urlresolvers import reverse
from django.http import Http404
from django.views.decorators.cache import never_cache

from sentry import http
from sentry.models import Integration, Identity, IdentityProvider, IdentityStatus, Organization
from sentry.utils.http import absolute_uri
from sentry.utils.signing import sign, unsign
from sentry.web.frontend.base import BaseView
from sentry.web.helpers import render_to_response

from .utils import logger


def build_linking_url(integration, organization, slack_id, channel_id, response_url):
    signed_params = sign(
        integration_id=integration.id,
        organization_id=organization.id,
        slack_id=slack_id,
        channel_id=channel_id,
        response_url=response_url,
    )

    return absolute_uri(reverse('sentry-integration-slack-link-identity', kwargs={
        'signed_params': signed_params,
    }))


class SlackLinkIdentitiyView(BaseView):
    @never_cache
    def handle(self, request, signed_params):
        params = unsign(signed_params.encode('ascii', errors='ignore'))

        try:
            organization = Organization.objects.get(
                id__in=request.user.get_orgs(),
                id=params['organization_id'],
            )
        except Organization.DoesNotExist:
            raise Http404

        try:
            integration = Integration.objects.get(
                id=params['integration_id'],
                organizations=organization,
            )
        except Integration.DoesNotExist:
            raise Http404

        try:
            idp_new = IdentityProvider.objects.get(
                external_id=integration.external_id,
                type='slack',
                organization_id=0,
            )
        except IdentityProvider.DoesNotExist:
            idp_new = None

        try:
            idp_old = IdentityProvider.objects.get(
                external_id=integration.external_id,
                type='slack',
                organization_id=organization.id,
            )
        except IdentityProvider.DoesNotExist:
            idp_old = None

        if not idp_new and not idp_old:
            raise Http404

        if request.method != 'POST':
            return render_to_response('sentry/auth-link-identity.html', request=request, context={
                'organization': organization,
                'provider': integration.get_provider(),
            })

        # TODO(epurkhiser): We could do some fancy slack querying here to
        # render a nice linking page with info about the user their linking.

        # Link the user with the identity. Handle the case where the user is linked to a
        # different identity or the identity is linked to a different user.
        # NOTE: during the IDP migration update both the old and new sets of identities.
        for idp in filter(None, (idp_new, idp_old)):
            try:
                id_by_user = Identity.objects.get(user=request.user, idp=idp)
            except Identity.DoesNotExist:
                id_by_user = None
            try:
                id_by_external_id = Identity.objects.get(external_id=params['slack_id'], idp=idp)
            except Identity.DoesNotExist:
                id_by_external_id = None

            if not id_by_user and not id_by_external_id:
                Identity.objects.create(
                    user=request.user,
                    external_id=params['slack_id'],
                    idp=idp,
                    status=IdentityStatus.VALID,
                )
            elif id_by_user and not id_by_external_id:
                # TODO(epurkhiser): In this case we probably want to prompt and
                # warn them that they had a previous identity linked to slack.
                id_by_user.update(
                    external_id=params['slack_id'],
                    status=IdentityStatus.VALID,
                )
            elif id_by_external_id and not id_by_user:
                id_by_external_id.update(
                    user=request.user,
                    status=IdentityStatus.VALID,
                )
            else:
                updates = {'status': IdentityStatus.VALID}
                if id_by_user != id_by_external_id:
                    id_by_external_id.delete()
                    updates['external_id'] = params['slack_id']
                id_by_user.update(**updates)

        payload = {
            'replace_original': False,
            'response_type': 'ephemeral',
            'text': "Your Slack identity has been linked to your Sentry account. You're good to go!"
        }

        session = http.build_session()
        req = session.post(params['response_url'], json=payload)
        resp = req.json()

        # If the user took their time to link their slack account, we may no
        # longer be able to respond, and we're not guaranteed able to post into
        # the channel. Ignore Expired url errors.
        #
        # XXX(epurkhiser): Yes the error string has a space in it.
        if not resp.get('ok') and resp.get('error') != 'Expired url':
            logger.error('slack.link-notify.response-error', extra={'response': resp})

        return render_to_response('sentry/slack-linked.html', request=request, context={
            'channel_id': params['channel_id'],
            'team_id': integration.external_id,
        })
