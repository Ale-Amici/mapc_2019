(define (problem problem-agentA2)
	(:domain agentA2)
	(:init 
		(can_submit)
		(at_goal_area)
		( = (points) 0.000000 )
		( = (costs) 0)
	)
	(:goal (and ( >= (points) 1.000000)))
	(:metric minimize (costs))
)
