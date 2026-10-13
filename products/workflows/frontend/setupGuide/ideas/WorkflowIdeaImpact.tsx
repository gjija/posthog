import { humanFriendlyNumber } from 'lib/utils/numbers'

import type { WorkflowIdeaApi } from '../../generated/api.schemas'
import { EXAMPLE_LIFT, exampleExtraPerMonth } from './ideaCopy'

/** Who an idea's workflow would reach each month, and what a small lift would be worth. */
export function WorkflowIdeaImpact({ evidence }: { evidence: WorkflowIdeaApi['evidence'] }): JSX.Element {
    const extra = exampleExtraPerMonth(evidence)
    const goal = evidence.goal ?? 'reach the goal'
    return (
        <div className="flex flex-col gap-0.5 text-sm" data-attr="workflow-idea-impact">
            <span>
                <strong className="text-base" translate="no">
                    {humanFriendlyNumber(evidence.reachable_people)}
                </strong>{' '}
                {evidence.reachable_people === 1
                    ? `person doesn't ${goal} each month`
                    : `${evidence.audience ?? 'people'} don't ${goal} each month`}
            </span>
            {!!extra && evidence.goal_unit && (
                <span className="text-secondary">
                    About{' '}
                    <strong className="text-success">
                        {humanFriendlyNumber(extra)} more {evidence.goal_unit}
                    </strong>{' '}
                    a month <span className="whitespace-nowrap">if {Math.round(EXAMPLE_LIFT * 100)} in 100 do</span>
                </span>
            )}
        </div>
    )
}
