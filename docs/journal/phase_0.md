# Project Description

This project combines GraphRAG with risk analysis for nuclear power plants. The goal is to be able to answer queries which allow users to perform failure analysis on a nuclear power plant. Using GraphRAG enables multi hop reasoning when answering queries. GraphRAG also helps to allow for answering global or “big picture” questions about nuclear risk. GraphRAG is built on top of a knowledge graph (KG). This knowledge graph is going to be constructed from ingesting the Nuclear Regulatory Commission’s (NRC) database for Licensed Event Reports (LERS). An example of a node in the KG might be a real life power plant, and an example of an edge might be occured_at for events that took place. This project is somewhat born out of an interest in dynamic probabilistic risk and analysis for safety critical systems, specifically nuclear power plants. This field has a really interesting mix of machine learning and statistics in order to properly model failure events and reduce risk. I’ve been wanting to make a personal project which uses some form of RAG for a while. I think that this project is definitely interesting to me because it demonstrates an instance where GraphRAG cleanly outperforms traditional RAG.

# Golden Questions

(Examples of questions I want this project to be able to answer)

1. What chain of failures led to HPCI being inoperable at the Quad Cities Power Plant? (MVP-now)
2. Which events across all these plants trace back to a weak maintenance or procedure program? (MVP-now)
3. What components have failed in the HPCI system across the whole corpus? (MVP-now)
4. Which events were mitigated by a redundant safety system being available? (MVP-now)
5. Given {X} component degrades, what is the most probably path to a safety consequence?
6. What combination of component failures have produced fuel cladding failures? (needs broader corpus)
7. What consequences followed steam generator tube degradation in past events, and how were they resolved?
8. What power plants reported failed containment leak tests and what was the result? (needs broader corpus)
9. Have there been cases where a power plant reported a missing fire barrier? (needs broader corpus)
10. In power plants to experienced failures of reactor fuel rod cladding what were the consequences and eventual remedies? (needs broader corpus)
11. What's the distribution of cause categories or event types across nuclear event reports? (MVP-now)
12. What sequence of events tend to lead to loss of offsite power? (needs broader corpus)
13. Find events at different plants that share both a common component and a common cause. (MVP-now)
14. For the HPCI system, group all corpus events by failure mode and show the most common one. (MVP-now)
