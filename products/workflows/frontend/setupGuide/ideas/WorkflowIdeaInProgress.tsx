import { useValues } from 'kea'

import { IconEllipsis, IconWarning } from '@posthog/icons'
import { LemonButton, LemonMenu, LemonTag, Tooltip } from '@posthog/lemon-ui'

import { copyToClipboard } from 'lib/utils/copyToClipboard'
import { humanFriendlyNumber } from 'lib/utils/numbers'
import { pluralize } from 'lib/utils/strings'
import { urls } from 'scenes/urls'

import type { WorkflowIdeaApi } from '../../generated/api.schemas'
import { onboardingWizardUrl } from '../wizard/onboardingWizardSteps'
import { ideaEmails } from './ideaCopy'
import { workflowIdeasLogic } from './workflowIdeasLogic'

/** A used idea whose workflow is still a draft, as one row with its single next step. */
export function WorkflowIdeaInProgress({ idea, hogFlowId }: { idea: WorkflowIdeaApi; hogFlowId: string }): JSX.Element {
    const { emailSender } = useValues(workflowIdeasLogic)
    const { evidence } = idea
    const needsSender = emailSender.status === 'none' || emailSender.status === 'unverified'
    const senderUrl =
        emailSender.status === 'none'
            ? onboardingWizardUrl('messaging', { step: 'channel' })
            : urls.workflows('channels')
    const continueUrl = urls.workflow(hogFlowId, 'workflow')

    const status = idea.reached_since_used
        ? `${humanFriendlyNumber(idea.reached_since_used)} ${idea.reached_since_used === 1 ? 'person' : (evidence.audience ?? 'people')} would have got the first email since you saved it`
        : `Draft with ${pluralize(ideaEmails(idea.definition).length, 'email')}, not sending yet`

    return (
        <div
            className="flex flex-col gap-2 rounded border bg-surface-primary px-3 py-2 @2xl:flex-row @2xl:items-center @2xl:gap-3"
            data-attr="workflow-idea-in-progress"
        >
            <div className="flex min-w-0 flex-1 flex-col gap-1 @2xl:flex-row @2xl:items-center @2xl:gap-3">
                <LemonTag type="completion" className="self-start @2xl:self-auto">
                    In progress
                </LemonTag>
                <span className="flex min-w-0 items-center gap-1 font-semibold">
                    <span>{idea.title}</span>
                    {evidence.review_note && (
                        <Tooltip title={evidence.review_note}>
                            <IconWarning className="shrink-0 text-warning" />
                        </Tooltip>
                    )}
                </span>
                <span className="text-sm text-secondary" data-attr="workflow-idea-reached-since">
                    {status}
                </span>
            </div>
            <div className="flex items-center gap-1">
                {needsSender ? (
                    <>
                        <LemonButton
                            type="primary"
                            size="small"
                            to={senderUrl}
                            // pinned: data-attr - autocapture dashboards read it
                            data-attr={
                                emailSender.status === 'none'
                                    ? 'workflow-idea-set-up-email'
                                    : 'workflow-idea-verify-domain'
                            }
                        >
                            {emailSender.status === 'none' ? 'Set up email sending' : 'Finish verifying your domain'}
                        </LemonButton>
                        <LemonMenu
                            items={[
                                {
                                    label: 'Continue setup',
                                    to: continueUrl,
                                    // pinned: data-attr - autocapture dashboards read it
                                    'data-attr': 'workflow-idea-continue',
                                },
                                {
                                    label: 'Copy link for a teammate',
                                    tooltip: "Whoever manages your domain's DNS can add the records from this page.",
                                    onClick: () =>
                                        void copyToClipboard(
                                            `${window.location.origin}${senderUrl}`,
                                            'link to the email sender setup'
                                        ),
                                    // pinned: data-attr - autocapture dashboards read it
                                    'data-attr': 'workflow-idea-copy-sender-link',
                                },
                            ]}
                        >
                            <LemonButton
                                size="small"
                                icon={<IconEllipsis />}
                                aria-label="More actions"
                                data-attr="workflow-idea-in-progress-more"
                            />
                        </LemonMenu>
                    </>
                ) : (
                    <LemonButton
                        type="primary"
                        size="small"
                        to={continueUrl}
                        // pinned: data-attr - autocapture dashboards read it
                        data-attr="workflow-idea-continue"
                    >
                        Continue setup
                    </LemonButton>
                )}
            </div>
        </div>
    )
}
