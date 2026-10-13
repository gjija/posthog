import { useActions, useValues } from 'kea'

import { IconSparkles, IconWarning } from '@posthog/icons'
import { LemonButton, LemonCard, LemonDialog, LemonInput, LemonTag, LemonTextArea } from '@posthog/lemon-ui'

import { LemonField } from 'lib/lemon-ui/LemonField'
import { getAccessControlDisabledReason } from 'lib/utils/accessControlUtils'

import { AccessControlLevel, AccessControlResourceType } from '~/types'

import type { WorkflowIdeaApi } from '../../generated/api.schemas'
import { ideaEmails, normalizeSite } from './ideaCopy'
import { WorkflowIdeaEmailPreview } from './WorkflowIdeaEmailPreview'
import { WorkflowIdeaFacts } from './WorkflowIdeaFacts'
import { WorkflowIdeaImpact } from './WorkflowIdeaImpact'
import { workflowIdeasLogic } from './workflowIdeasLogic'

/** A workflow PostHog drafted for the project: who it reaches, what it could be worth, and how to use it. */
export function WorkflowIdeaCard({
    idea,
    recommended = false,
}: {
    idea: WorkflowIdeaApi
    recommended?: boolean
}): JSX.Element {
    const { busyId, siteInput } = useValues(workflowIdeasLogic)
    const { applyIdea, dismissIdea, setSiteInput } = useActions(workflowIdeasLogic)
    const busy = busyId === idea.id
    const { evidence } = idea
    const emails = ideaEmails(idea.definition)
    const noEditAccess = getAccessControlDisabledReason(AccessControlResourceType.Workflow, AccessControlLevel.Editor)

    const openPreview = (): void => {
        LemonDialog.open({
            title: idea.title,
            width: 680,
            content: (
                <div className="flex flex-col gap-4">
                    <WorkflowIdeaFacts evidence={evidence} emailCount={emails.length} />
                    {evidence.review_note && (
                        <div className="flex items-start gap-2 rounded border border-warning p-2 text-sm">
                            <IconWarning className="mt-0.5 shrink-0 text-warning" />
                            <span>{evidence.review_note}</span>
                        </div>
                    )}
                    <WorkflowIdeaEmailPreview emails={emails} waits={evidence.waits ?? []} />
                </div>
            ),
            primaryButton: { children: 'Close' },
        })
    }

    const openSiteStep = (): void => {
        LemonDialog.openForm({
            title: 'Where should the email buttons link?',
            description: 'Enter your website. You can point each button at an exact page in the editor later.',
            initialValues: { site: siteInput },
            content: (
                <LemonField name="site" label="Your website">
                    <LemonInput placeholder="example.com" autoFocus data-attr="workflow-idea-site-input" />
                </LemonField>
            ),
            errors: { site: (site: string) => (normalizeSite(site ?? '') ? undefined : 'Enter a website address') },
            primaryButtonProps: { children: 'Use this workflow' },
            onSubmit: ({ site }) => {
                setSiteInput(site)
                applyIdea(idea)
            },
        })
    }

    const openDismiss = (): void => {
        LemonDialog.openForm({
            title: 'Dismiss this idea?',
            description: "PostHog won't suggest it again in this project.",
            initialValues: { reason: '' },
            content: (
                <LemonField name="reason" label="Why not? (optional)">
                    <LemonTextArea
                        placeholder="For example: we already send this from our own app"
                        maxLength={2000}
                        // The dialog form submits on Enter, which would dismiss before the reason is finished.
                        onKeyDown={(e) => e.key === 'Enter' && e.stopPropagation()}
                        data-attr="workflow-idea-dismiss-reason"
                    />
                </LemonField>
            ),
            primaryButtonProps: { children: 'Dismiss' },
            onSubmit: ({ reason }) => dismissIdea(idea, reason ?? ''),
        })
    }

    return (
        <LemonCard hoverEffect={false} className="@container h-full p-4" data-attr="workflow-idea">
            {/* A lone card spans the whole row, so it lays out side by side rather than as a tall column. */}
            <div className="flex h-full flex-col gap-3 @2xl:flex-row @2xl:items-center @2xl:gap-6">
                <div className="flex flex-1 flex-col gap-3">
                    <div className="flex flex-wrap items-center gap-2">
                        <LemonTag type="highlight" icon={<IconSparkles />}>
                            Drafted by PostHog
                        </LemonTag>
                        {recommended && <LemonTag type="success">Start here</LemonTag>}
                    </div>
                    <div className="flex flex-col gap-1">
                        <h4 className="mb-0 text-base font-semibold">{idea.title}</h4>
                        <p className="mb-0 line-clamp-2 text-sm text-secondary">{idea.rationale}</p>
                    </div>
                    <WorkflowIdeaImpact evidence={evidence} />
                </div>
                <div className="mt-auto flex flex-wrap items-center gap-2 @2xl:mt-0 @2xl:shrink-0">
                    <LemonButton
                        type="primary"
                        size="small"
                        loading={busy}
                        disabledReason={noEditAccess ?? (busyId && !busy ? 'Another idea is being saved' : undefined)}
                        onClick={() => (evidence.site_url ? applyIdea(idea) : openSiteStep())}
                        // pinned: data-attr - autocapture dashboards read it
                        data-attr="workflow-idea-use"
                    >
                        Use this workflow
                    </LemonButton>
                    <LemonButton
                        type="secondary"
                        size="small"
                        onClick={openPreview}
                        // pinned: data-attr - autocapture dashboards read it
                        data-attr="workflow-idea-preview-open"
                    >
                        Preview
                    </LemonButton>
                    <LemonButton
                        type="tertiary"
                        size="small"
                        disabledReason={noEditAccess ?? (busyId ? 'An idea is being saved' : undefined)}
                        onClick={openDismiss}
                        // pinned: data-attr - autocapture dashboards read it
                        data-attr="workflow-idea-dismiss"
                    >
                        Dismiss
                    </LemonButton>
                </div>
            </div>
        </LemonCard>
    )
}
