Databastransaktioner och isolering

En transaktion är en sekvens av databasoperationer som körs som en atomär enhet.
Antingen genomförs alla operationer eller ingen. Transaktionen avslutas med commit,
som gör ändringarna permanenta, eller rollback, som återställer allt.

ACID beskriver fyra garantier. Atomicity: allt eller inget. Consistency: databasen
går från ett giltigt tillstånd till ett annat. Isolation: samtidiga transaktioner
stör inte varandra. Durability: en committad transaktion överlever en krasch.

Isolationsnivåer styr hur mycket samtidiga transaktioner får se av varandra.
READ UNCOMMITTED tillåter dirty reads. READ COMMITTED läser bara committad data
men tillåter non-repeatable reads. REPEATABLE READ garanterar att en läst rad ser
likadan ut resten av transaktionen. SERIALIZABLE ger samma resultat som om
transaktionerna körts en i taget, till priset av lägre samtidighet.

En deadlock uppstår när två transaktioner cykliskt väntar på lås som den andra
håller. Databasen upptäcker cykeln och avbryter en av transaktionerna.
