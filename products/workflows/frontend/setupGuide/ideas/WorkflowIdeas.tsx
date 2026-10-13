import clsx from 'clsx'
import { useValues } from 'kea'

import { IconSparkles } from '@posthog/icons'
import { LemonSkeleton } from '@posthog/lemon-ui'

import { FEATURE_FLAGS } from 'lib/constants'
import { featureFlagLogic } from 'lib/logic/featureFlagLogic'

import { WorkflowIdeaCard } from './WorkflowIdeaCard'
import { WorkflowIdeaInProgress } from './WorkflowIdeaInProgress'
import { workflowIdeasLogic } from './workflowIdeasLogic'

/** Workflows PostHog drafted for this project from its own events, shown until each is used or dismissed. */
export function WorkflowIdeas(): JSX.Element | null {
    const { featureFlags } = useValues(featureFlagLogic)
    return featureFlags[FEATURE_FLAGS.WORKFLOWS_IDEAS] ? <WorkflowIdeasSection /> : null
}

function WorkflowIdeasSection(): JSX.Element | null {
    const { ideas, ideasLoading } = useValues(workflowIdeasLogic)

    if (ideas === null && ideasLoading) {
        return (
            <div className="grid grid-cols-1 gap-3 @4xl:grid-cols-3">
                {[0, 1, 2].map((index) => (
                    <LemonSkeleton key={index} className="h-36 rounded" />
                ))}
            </div>
        )
    }
    if (!ideas?.length) {
        return null
    }
    const inProgress = ideas.filter((idea) => idea.status === 'accepted' && idea.hog_flow_id)
    // Three ideas at most, so the row never wraps. Using or dismissing one brings in the next.
    const suggested = ideas.filter((idea) => idea.status === 'suggested').slice(0, 3)

    return (
        <section
            // Tints the panel with the Workflows product color, which has no ready-made background token.
            className="@container mb-4 flex flex-col gap-3 rounded-lg border p-4 bg-[color-mix(in_srgb,var(--color-product-workflows-light)_10%,transparent)]"
            data-attr="workflow-ideas"
        >
            <div className="flex items-start gap-2">
                <IconSparkles className="mt-0.5 size-5 shrink-0 text-accent" />
                <div className="flex flex-col">
                    <h3 className="mb-0 text-base font-semibold">Workflows drafted for you</h3>
                    <span className="text-xs text-secondary">
                        Built from events your project already sends. Nothing is sent until you turn a workflow on.
                    </span>
                </div>
            </div>
            {inProgress.length > 0 && (
                <div className="flex flex-col gap-2">
                    {inProgress.map((idea) => (
                        <WorkflowIdeaInProgress key={idea.id} idea={idea} hogFlowId={idea.hog_flow_id as string} />
                    ))}
                </div>
            )}
            {suggested.length > 0 && (
                <div
                    className={clsx(
                        'grid grid-cols-1 gap-3',
                        // Columns only once every card fits on one row, so a third card never sits alone on a row.
                        suggested.length === 2 && '@2xl:grid-cols-2',
                        suggested.length === 3 && '@4xl:grid-cols-3'
                    )}
                >
                    {suggested.map((idea, index) => (
                        <WorkflowIdeaCard
                            key={idea.id}
                            idea={idea}
                            // The list puts the best first bet first. A lone idea or a draft in progress needs no pointer.
                            recommended={index === 0 && suggested.length > 1 && inProgress.length === 0}
                        />
                    ))}
                </div>
            )}
        </section>
    )
}
