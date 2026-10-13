import { humanFriendlyNumber } from 'lib/utils/numbers'
import { pluralize } from 'lib/utils/strings'

import type { WorkflowIdeaApi } from '../../generated/api.schemas'
import { estimatedMonthlyCost, waitLabel } from './ideaCopy'

/** What an idea's workflow does in full: its trigger, schedule, exit, entry limit and email volume. */
export function WorkflowIdeaFacts({
    evidence,
    emailCount,
}: {
    evidence: WorkflowIdeaApi['evidence']
    emailCount: number
}): JSX.Element {
    const waits = evidence.waits ?? []
    const schedule =
        waits.length === emailCount && waits.length > 0
            ? `, ${waits.map((wait, index) => (index === 0 ? `${waitLabel(wait)} later` : `then ${waitLabel(wait)} after that`)).join(', ')}`
            : ''
    const emailsPerMonth = evidence.emails_per_month ?? 0
    const cost = estimatedMonthlyCost(emailsPerMonth)

    return (
        <dl className="m-0 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
            <dt className="text-secondary">Starts</dt>
            <dd className="m-0">
                When someone sends{' '}
                <code className="text-xs" translate="no">
                    {evidence.trigger_event}
                </code>
            </dd>
            <dt className="text-secondary">Sends</dt>
            <dd className="m-0">
                {pluralize(emailCount, 'email')}
                {schedule}
            </dd>
            {evidence.goal && (
                <>
                    <dt className="text-secondary">Stops</dt>
                    <dd className="m-0">As soon as they {evidence.goal}</dd>
                </>
            )}
            {evidence.once_per_person_days ? (
                <>
                    <dt className="text-secondary">Limit</dt>
                    <dd className="m-0">Once per person every {evidence.once_per_person_days} days</dd>
                </>
            ) : null}
            {emailsPerMonth ? (
                <>
                    <dt className="text-secondary">Volume</dt>
                    <dd className="m-0">
                        About {humanFriendlyNumber(emailsPerMonth)} emails a month,{' '}
                        {cost > 0 ? `roughly $${humanFriendlyNumber(cost)} a month` : 'within the free monthly emails'}
                    </dd>
                </>
            ) : null}
        </dl>
    )
}
